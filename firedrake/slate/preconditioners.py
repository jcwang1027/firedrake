"""This module provides custom python preconditioners utilizing
the Slate language.
"""

from __future__ import absolute_import, print_function, division

import ufl

from firedrake.matrix_free.preconditioners import PCBase
from firedrake.petsc import PETSc
from firedrake.slate.slate import Tensor


__all__ = ['HybridizationPC']


class HybridizationPC(PCBase):
    """A Slate-based python preconditioner that solves a
    mixed saddle-point problem using hybridization.

    The forward eliminations and backwards reconstructions
    are performed element-local using the Slate language.
    """
    def initialize(self, pc):
        """Set up the problem context. Take the original
        mixed problem and reformulate the problem as a
        hybridized mixed system.

        A KSP is created for the Lagrange multiplier system.
        """
        from ufl.algorithms.replace import replace
        from firedrake import (FunctionSpace, TrialFunction,
                               TrialFunctions, TestFunction, Function,
                               BrokenElement, MixedElement,
                               FacetNormal, Constant, DirichletBC,
                               Projector)
        from firedrake.assemble import (allocate_matrix,
                                        create_assembly_callable)
        from firedrake.formmanipulation import split_form

        # Extract the problem context
        prefix = pc.getOptionsPrefix()
        _, P = pc.getOperators()
        context = P.getPythonContext()
        test, trial = context.a.arguments()

        V = test.function_space()
        mesh = V.mesh()
        if mesh.cell_set._extruded:
            # TODO: Merge FIAT branch to support TPC trace elements
            raise NotImplementedError("Not implemented on extruded meshes.")

        assert len(V) == 2, (
            "Can only hybridize a mixed system with two spaces."
        )

        # TODO: Future update to include more general spaces?
        if all(Vi.ufl_element().value_shape() for Vi in V):
            raise ValueError(
                "Expecting an H(div) x L2 pair of spaces. "
                "Both spaces cannot be vector-valued."
            )

        # Create the space of approximate traces.
        # TODO: Once extruded and tensor product trace elements
        # are ready, this logic will be updated.
        W, = (v for v in V if v.ufl_element().value_shape())
        if W.ufl_element().family() == "Raviart-Thomas":
            tdegree = W.ufl_element().degree() - 1

        elif W.ufl_element().family() == "Brezzi-Douglas-Marini":
            tdegree = W.ufl_element().degree()

        else:
            raise ValueError(
                "%s not supported at the moment." % W.ufl_element().family()
            )

        TraceSpace = FunctionSpace(mesh, "HDiv Trace", tdegree)

        # NOTE: For extruded, we will need to add "on_top" and "on_bottom"
        trace_conditions = [DirichletBC(TraceSpace, Constant(0.0),
                                        "on_boundary")]

        # Break the function spaces and define fully discontinuous spaces
        broken_elements = MixedElement([BrokenElement(Vi.ufl_element())
                                        for Vi in V])
        V_d = FunctionSpace(mesh, broken_elements)

        # Set up the functions for the original, hybridized
        # and schur complement systems
        self.broken_solution = Function(V_d)
        self.broken_rhs = Function(V_d)
        self.trace_solution = Function(TraceSpace)
        self.unbroken_solution = Function(V)
        self.unbroken_rhs = Function(V)

        arg_map = {test: TestFunction(V_d),
                   trial: TrialFunction(V_d)}

        # Create the symbolic Schur-reduction:
        # Original mixed operator replaced with "broken"
        # arguments
        Atilde = Tensor(replace(context.a, arg_map))
        gammar = TestFunction(TraceSpace)
        n = FacetNormal(mesh)

        # Vector trial function will have a non-empty ufl_shape
        sigma, = (f for f in TrialFunctions(V_d) if f.ufl_shape)

        # NOTE: Once extruded is ready, this will change slightly
        # to include both horizontal and vertical interior facets
        K = Tensor(gammar('+') * ufl.dot(sigma, n) * ufl.dS)

        # Assemble the Schur complement operator and right-hand side
        self.schur_rhs = Function(TraceSpace)
        self._assemble_Srhs = create_assembly_callable(
            K * Atilde.inv * self.broken_rhs,
            tensor=self.schur_rhs,
            form_compiler_parameters=context.fc_params)

        schur_comp = K * Atilde.inv * K.T
        self.S = allocate_matrix(schur_comp,
                                 bcs=trace_conditions,
                                 form_compiler_parameters=context.fc_params)
        self._assemble_S = create_assembly_callable(
            schur_comp,
            tensor=self.S,
            bcs=trace_conditions,
            form_compiler_parameters=context.fc_params)

        self._assemble_S()
        self.S.force_evaluation()
        Smat = self.S.petscmat

        # Nullspace for the multiplier problem
        nullspace = create_schur_nullspace(P, K * Atilde.inv,
                                           V, V_d, TraceSpace,
                                           pc.comm)
        if nullspace:
            Smat.setNullSpace(nullspace)

        # Set up the KSP for the system of Lagrange multipliers
        ksp = PETSc.KSP().create(comm=pc.comm)
        ksp.setOptionsPrefix(prefix + "hybridization_")
        ksp.setOperators(Smat)
        ksp.setUp()
        ksp.setFromOptions()
        self.ksp = ksp

        # Now we construct the local tensors for the reconstruction stage
        # TODO: Add support for mixed tensors and these variables
        # become unnecessary
        split_forms = dict(split_form(Atilde.form))
        A = Tensor(split_forms[(0, 0)])
        B = Tensor(split_forms[(0, 1)])
        C = Tensor(split_forms[(1, 0)])
        D = Tensor(split_forms[(1, 1)])
        trial = TrialFunction(FunctionSpace(mesh,
                                            BrokenElement(W.ufl_element())))
        K_local = Tensor(gammar('+') * ufl.dot(trial, n) * ufl.dS)

        # Split functions and reconstruct each bit separately
        sigma_h, u_h = self.broken_solution.split()
        g, f = self.broken_rhs.split()

        # Pressure reconstruction
        M = D - C * A.inv * B
        u_sol = M.inv * f + M.inv * (C * A.inv *
                                     K_local.T * self.trace_solution
                                     - C * A.inv * g)
        self._assemble_pressure = create_assembly_callable(
            u_sol,
            tensor=u_h,
            form_compiler_parameters=context.fc_params)

        # Velocity reconstruction
        sigma_sol = A.inv * g - A.inv * (B * u_h +
                                         K_local.T * self.trace_solution)
        self._assemble_velocity = create_assembly_callable(
            sigma_sol,
            tensor=sigma_h,
            form_compiler_parameters=context.fc_params)

        # Set up the projectors
        vector_index = list(V).index(W)
        broken_vec_data = self.broken_rhs.split()[vector_index]
        unbroken_vec_data = self.broken_rhs.split()[vector_index]
        self.data_projector = Projector(unbroken_vec_data,
                                        broken_vec_data)

        # NOTE: Tolerance is very important here and so we provide
        # the user a way to specify projector tolerance
        opts = PETSc.Options()
        tol = opts.getReal(prefix+'hybridization_projector_tolerance', 1e-8)
        broken_vel = self.broken_solution.split()[vector_index]
        unbroken_vel = self.unbroken_solution.split()[vector_index]
        self.projector = Projector(broken_vel,
                                   unbroken_vel,
                                   solver_parameters={"ksp_type": "cg",
                                                      "ksp_rtol": tol})

    def update(self, pc):
        """Update by assembling into the operator. No need to
        reconstruct symbolic objects.
        """
        self._assemble_S()
        self.S.force_evaluation()
        self._assemble_Srhs()

    def apply(self, pc, x, y):
        """We solve the forward eliminated problem for the
        approximate traces of the scalar solution (the multipliers)
        and reconstruct the "broken flux and scalar variable."

        Lastly, we project the broken solutions into the mimetic
        non-broken finite element space.
        """

        # Transfer non-broken x into a firedrake function
        with self.unbroken_rhs.dat.vec as v:
            x.copy(v)

        # Transfer unbroken_rhs into broken_rhs
        unbroken_scalar_field, = (f for f in self.unbroken_rhs.split()
                                  if not f.ufl_shape)
        broken_scalar_field, = (f for f in self.broken_rhs.split()
                                if not f.ufl_shape)

        # This updates broken_rhs
        self.data_projector.project()
        unbroken_scalar_field.dat.copy(broken_scalar_field.dat)

        # Compute the rhs for the multiplier system
        self._assemble_Srhs()

        # Solve the system for the Lagrange multipliers
        with self.schur_rhs.dat.vec_ro as b:
            with self.trace_solution.dat.vec as x:
                self.ksp.solve(b, x)

        # Assemble the pressure and velocity (in that order)
        # using the Lagrange multipliers
        self._assemble_pressure()
        self._assemble_velocity()

        # Project the broken solution into non-broken spaces
        broken_pressure, = (p for p in self.broken_solution.split()
                            if not p.ufl_shape)
        unbroken_pressure, = (p for p in self.unbroken_solution.split()
                              if not p.ufl_shape)
        broken_pressure.dat.copy(unbroken_pressure.dat)
        self.projector.project()
        with self.unbroken_solution.dat.vec_ro as v:
            v.copy(y)

    def applyTranspose(self, pc, x, y):
        """Apply the transpose of the preconditioner."""
        raise NotImplementedError(
            "The transpose application of this PC"
            "is not implemented."
        )

    def view(self, pc, viewer=None):
        super(HybridizationPC, self).view(pc, viewer)
        viewer.printfASCII("Solves K * P^-1 * K.T using local eliminations.\n")
        viewer.pushASCIITab()
        viewer.printfASCII("KSP solver for the multipliers:\n")
        viewer.pushASCIITab()
        self.ksp.view(viewer)
        viewer.popASCIITab()


def create_schur_nullspace(P, forward, V, V_d, TraceSpace, comm):
    """Gets the nullspace vectors corresponding to the Schur complement
    system for the multipliers.

    :arg P: The mixed operator from the ImplicitMatrixContext.
    :arg forward: A Slate expression denoting the forward elimination
                  operator.
    :arg V: The original "unbroken" space.
    :arg V_d: The broken space.
    :arg TraceSpace: The space of approximate traces.

    Returns: A nullspace (if there is one) for the Schur-complement system.
    """
    from firedrake import project, assemble, Function

    nullspace = P.getNullSpace()
    if nullspace.handle == 0:
        # No nullspace
        return None

    vecs = nullspace.getVecs()
    tmp = Function(V)
    tmp_b = Function(V_d)
    tnsp_tmp = Function(TraceSpace)
    forward_action = forward * tmp_b
    new_vecs = []
    for v in vecs:
        with tmp.dat.vec as t:
            v.copy(t)

        project(tmp, tmp_b)
        assemble(forward_action, tensor=tnsp_tmp)
        with tnsp_tmp.dat.vec_ro as v:
            new_vecs.append(v.copy())

    schur_nullspace = PETSc.NullSpace().create(vectors=new_vecs,
                                               comm=comm)
    return schur_nullspace
