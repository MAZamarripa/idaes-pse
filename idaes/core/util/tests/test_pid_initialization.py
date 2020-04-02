##############################################################################
# Institute for the Design of Advanced Energy Systems Process Systems
# Engineering Framework (IDAES PSE Framework) Copyright (c) 2018-2019, by the
# software owners: The Regents of the University of California, through
# Lawrence Berkeley National Laboratory,  National Technology & Engineering
# Solutions of Sandia, LLC, Carnegie Mellon University, West Virginia
# University Research Corporation, et al. All rights reserved.
#
# Please see the files COPYRIGHT.txt and LICENSE.txt for full copyright and
# license information, respectively. Both files are also available online
# at the URL "https://github.com/IDAES/idaes-pse".
##############################################################################
"""
Tests for math util methods.
"""

import pytest
from pyomo.environ import (Block, ConcreteModel, Constraint, Expression,
                           Set, SolverFactory, Var, value, Param, Reals,
                           TransformationFactory, TerminationCondition,
                           exp)
from pyomo.network import Arc, Port

from idaes.core import (FlowsheetBlock, 
                        MaterialBalanceType, 
                        EnergyBalanceType,
                        MomentumBalanceType, 
                        declare_process_block_class,
                        PhysicalParameterBlock,
                        StateBlock,
                        StateBlockData,
                        ReactionParameterBlock,
                        ReactionBlockBase,
                        ReactionBlockDataBase,
                        MaterialFlowBasis)
from idaes.core.util.testing import PhysicalParameterTestBlock
from idaes.core.util.model_statistics import degrees_of_freedom
from idaes.generic_models.unit_models import CSTR
from idaes.core.util.exceptions import ConfigurationError
from idaes.core.util.initialization import (fix_state_vars,
                                            revert_state_vars,
                                            propagate_state,
                                            solve_indexed_blocks,
                                            initialize_by_time_element)

__author__ = "Andrew Lee"


# See if ipopt is available and set up solver
if SolverFactory('ipopt').available():
    solver = SolverFactory('ipopt')
    solver.options = {'tol': 1e-6,
                      'mu_init': 1e-8,
                      'bound_push': 1e-8}
else:
    solver = None


@declare_process_block_class("AqueousEnzymeParameterBlock")
class ParameterData(PhysicalParameterBlock):
    """
    Parameter block for the aqueous enzyme reaction in the biochemical CSTR
    used by Christofides and Daoutidis, 1996, presented by Heineken et al, 1967
    """
    def build(self):
        super(ParameterData, self).build()

        # all components are in the aqueous phase
        self.phase_list = Set(initialize=['aq'])
        self.component_list = Set(initialize=['S', 'E', 'C', 'P'])

        self.state_block_class = AqueousEnzymeStateBlock

    @classmethod
    def define_metadata(cls, obj):
        obj.add_default_units({'time': 'min',
                               'length': 'm',
                               'amount': 'kmol',
                               'temperature': 'K',
                               'energy': 'kcal',
                               'holdup': 'kmol'})


class _AqueousEnzymeStateBlock(StateBlock):
    def initialize(blk):
        pass

@declare_process_block_class("AqueousEnzymeStateBlock",
                             block_class=_AqueousEnzymeStateBlock)
class AqueousEnzymeStateBlockData(StateBlockData):
    def build(self):
        super(AqueousEnzymeStateBlockData, self).build()

        self.conc_mol = Var(self._params.component_list,
                             domain=Reals,
                             doc='Component molar concentration [kmol/m^3]')
        
        self.flow_mol_comp = Var(self._params.component_list,
                                 domain=Reals, 
                                 doc='Molar component flow rate [kmol/min]')

        self.flow_rate = Var(domain=Reals,
                             doc='Volumetric flow rate out of reactor [m^3/min]')

        self.temperature = Var(initialize=303, domain=Reals,
                               doc='Temperature within reactor [K]')

        def flow_mol_comp_rule(b, j):
            return b.flow_mol_comp[j] == b.flow_rate*b.conc_mol[j]
        
        self.flow_mol_comp_eqn = Constraint(self._params.component_list,
                rule=flow_mol_comp_rule,
                doc='Outlet component molar flow rate equation')

    def get_material_density_terms(b, p, j):
        return b.conc_mol[j]

    def get_material_flow_terms(b, p, j):
        return b.flow_mol_comp[j]

    def get_material_flow_basis(b):
        return MaterialFlowBasis.molar

    def get_enthalpy_flow_terms(b, p):
        return b.flow_rate*b.temperature

    def get_energy_density_terms(b, p):
        return b.temperature

    def define_state_vars(b):
        return {'conc_mol': b.conc_mol,
                'flow_mol_comp': b.flow_mol_comp,
                'temperature': b.temperature,
                'flow_rate': b.flow_rate}

@declare_process_block_class('EnzymeReactionParameterBlock')
class EnzymeReactionParameterData(ReactionParameterBlock):
    '''
    Enzyme reaction:
    S + E <-> C -> P + E
    '''
    def build(self):
        super(EnzymeReactionParameterData, self).build()

        self.reaction_block_class = EnzymeReactionBlock

        self.rate_reaction_idx = Set(initialize=['R1', 'R2', 'R3'])
        self.rate_reaction_stoichiometry = {('R1', 'aq', 'S'): -1,
                                            ('R1', 'aq', 'E'): -1,
                                            ('R1', 'aq', 'C'): 1,
                                            ('R1', 'aq', 'P'): 0,
                                            ('R2', 'aq', 'S'): 1,
                                            ('R2', 'aq', 'E'): 1,
                                            ('R2', 'aq', 'C'): -1,
                                            ('R2', 'aq', 'P'): 0,
                                            ('R3', 'aq', 'S'): 0,
                                            ('R3', 'aq', 'E'): 1,
                                            ('R3', 'aq', 'C'): -1,
                                            ('R3', 'aq', 'P'): 1}

        self.act_energy = Param(self.rate_reaction_idx,
                initialize={'R1': 8.0e3,
                            'R2': 9.0e3,
                            'R3': 1.0e4},
                doc='Activation energy [kcal/kmol]')

        self.gas_const = Param(initialize=1.987, 
                doc='Gas constant R [kcal/kmol/K]')

        self.temperature_ref = Param(initialize=300.0, doc='Reference temperature')

        self.k_rxn = Param(self.rate_reaction_idx,
                initialize={'R1': 3.36e6,
                            'R2': 1.80e6,
                            'R3': 5.79e7},
                doc='Pre-exponential rate constant in Arrhenius expression')

    #    self.reaction_block_class = EnzymeReactionBlock

    @classmethod
    def define_metadata(cls, obj):
        obj.add_default_units({'time': 'min',
                               'length': 'm',
                               'amount': 'kmol',
                               'energy': 'kcal'})

class _EnzymeReactionBlock(ReactionBlockBase):
    def initialize(blk):
        # initialize for reaction rates for each data object
        pass

@declare_process_block_class('EnzymeReactionBlock',
                             block_class=_EnzymeReactionBlock)
class EnzymeReactionBlockData(ReactionBlockDataBase):
    def build(self):
        super(EnzymeReactionBlockData, self).build()

        self.reaction_coef = Var(self._params.rate_reaction_idx,
                         domain=Reals, doc='Reaction rate coefficient')

        self.reaction_rate = Var(self._params.rate_reaction_idx,
                                 domain=Reals, 
                                 doc='Reaction rate [kmol/m^3/min]')

        self.dh_rxn = Param(self._params.rate_reaction_idx,
                domain=Reals, doc='Heat of reaction',
                initialize={'R1': 1e3/900/0.231,
                            'R2': 1e3/900/0.231,
                            'R3': 5e3/900/0.231})

        def reaction_rate_rule(b, r):
            if r == 'R1':
                return (b.reaction_rate[r] == 
                        b.reaction_coef[r]*
                        b.state_ref.conc_mol['S']*b.state_ref.conc_mol['E'])
            elif r == 'R2':
                return (b.reaction_rate[r] ==
                        b.reaction_coef[r]*
                        b.state_ref.conc_mol['C'])
            elif r == 'R3':
                return (b.reaction_rate[r] ==
                        b.reaction_coef[r]*
                        b.state_ref.conc_mol['C'])

        self.reaction_rate_eqn = Constraint(self._params.rate_reaction_idx,
                rule=reaction_rate_rule)

        def arrhenius_rule(b, r):
            return (b.reaction_coef[r] == b._params.k_rxn[r]*
                    exp(-b._params.act_energy[r]/b._params.gas_const/
                        b.state_ref.temperature))

        self.arrhenius_eqn = Constraint(self._params.rate_reaction_idx,
                rule=arrhenius_rule)
    
    def get_reaction_rate_basis(b):
        return MaterialFlowBasis.molar


@pytest.mark.skipif(solver is None, reason="Solver not available")
def test_initialize_by_time_element():
    horizon = 6
    time_set = [0, horizon]
    ntfe = 60 # For a finite element every six seconds
    ntcp = 2
    m = ConcreteModel(name='CSTR model for testing')
    m.fs = FlowsheetBlock(default={'dynamic': True,
                                   'time_set': time_set})

    m.fs.properties = AqueousEnzymeParameterBlock()
    m.fs.reactions = EnzymeReactionParameterBlock(
            default={'property_package': m.fs.properties})
    m.fs.cstr = CSTR(default={"property_package": m.fs.properties,
                              "reaction_package": m.fs.reactions,
                              "material_balance_type": MaterialBalanceType.componentTotal,
                              "energy_balance_type": EnergyBalanceType.enthalpyTotal,
                              "momentum_balance_type": MomentumBalanceType.none,
                              "has_heat_of_reaction": True})

    # Time discretization
    disc = TransformationFactory('dae.collocation')
    disc.apply_to(m, wrt=m.fs.time, nfe=ntfe, ncp=ntcp, scheme='LAGRANGE-RADAU')

    # Fix geometry variables
    m.fs.cstr.volume.fix(1.0)

    # Fix initial conditions:
    for p, j in m.fs.properties.phase_list*m.fs.properties.component_list:
        m.fs.cstr.control_volume.material_holdup[0, p, j].fix(0)

    # TODO: 
    #     - Split into mixer and CSTR with two distinct inlet streams
    #     - Add PID controller to outlet flow rate
    #     - Introduce perturbations in inlet flow rate
    #     - initialize_by_time_element

    # Fix inlet conditions
    # This is a huge hack because I didn't know that the proper way to
    # have multiple inlets to a CSTR was to use a mixer.
    # I 'combine' both my inlet streams before sending them to the CSTR.
    for t, j in m.fs.time*m.fs.properties.component_list:
        if t <= 2:
            if j == 'E':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(11.91*0.1)
            elif j == 'S':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(12.92*2.1)
            else:
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(0)
        elif t <= 4:
            if j == 'E':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(5.95*0.1)
            elif j == 'S':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(12.92*2.1)
            else:
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(0)
        else:
            if j == 'E':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(8.95*0.1)
            elif j == 'S':
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(16.75*2.1)
            else:
                m.fs.cstr.inlet.flow_mol_comp[t, j].fix(0)

    m.fs.cstr.inlet.flow_rate.fix(2.2)
    m.fs.cstr.inlet.temperature.fix(300)

    # Fix outlet conditions
    m.fs.cstr.outlet.flow_rate.fix(2.2)
    m.fs.cstr.outlet.temperature[m.fs.time.first()].fix(300)

    assert degrees_of_freedom(m) == 0

    initialize_by_time_element(m.fs, m.fs.time, solver=solver)

    assert degrees_of_freedom(m) == 0

    # Assert that the result looks how we expect
    assert m.fs.cstr.outlet.conc_mol[0, 'S'].value == 0
    assert abs(m.fs.cstr.outlet.conc_mol[2, 'S'].value - 11.389) < 1e-2
    assert abs(m.fs.cstr.outlet.conc_mol[4, 'P'].value - 0.2191) < 1e-3
    assert abs(m.fs.cstr.outlet.conc_mol[6, 'E'].value - 0.0327) < 1e-3
    assert abs(m.fs.cstr.outlet.temperature[6].value - 289.7) < 1

    # Assert that model is still fixed and deactivated as expected
    assert (
    m.fs.cstr.control_volume.material_holdup[m.fs.time.first(), 'aq', 'S'].fixed)

    for t in m.fs.time:
        if t != m.fs.time.first():
            assert (not 
    m.fs.cstr.control_volume.material_holdup[t, 'aq', 'S'].fixed)

            assert not m.fs.cstr.outlet.temperature[t].fixed
        assert (
    m.fs.cstr.control_volume.material_holdup_calculation[t, 'aq', 'C'].active)

        assert m.fs.cstr.control_volume.properties_out[t].active
        assert not m.fs.cstr.outlet.flow_mol_comp[t, 'S'].fixed
        assert m.fs.cstr.inlet.flow_mol_comp[t, 'S'].fixed

    # Assert that constraints are feasible after initialization
    for con in m.fs.component_data_objects(Constraint, active=True):
        assert value(con.body) - value(con.upper) < 1e-5
        assert value(con.lower) - value(con.body) < 1e-5

    results = solver.solve(m.fs)
    assert results.solver.termination_condition == TerminationCondition.optimal

if __name__ == '__main__':
    test_initialize_by_time_element()
