#
# Copyright 2021 Lars Pastewka (U. Freiburg)
#           2021 Jan Griesser (U. Freiburg)
#           2020-2021 Jonas Oldenstaedt (U. Freiburg)
#
# matscipy - Materials science with Python at the atomic-scale
# https://github.com/libAtoms/matscipy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

"""
Bond Order Potential.
"""

#
# Coding convention
# * All numpy arrays are suffixed with the array dimensions
# * The suffix stands for a certain type of dimension:
#   - n: Atomic index, i.e. array dimension of length nb_atoms
#   - p: Pair index, i.e. array dimension of length nb_pairs
#   - t: Triplet index, i.e. array dimension of length nb_triplets
#   - c: Cartesian index, array dimension of length 3
#   - a: Cartesian index for the first dimension of the deformation gradient, array dimension of length 3
#   - b: Cartesian index for the second dimension of the deformation gradient, array dimension of length 3
#

import numpy as np

from scipy.sparse.linalg import cg

import ase

from scipy.sparse import bsr_matrix

from ase.calculators.calculator import Calculator

from ...elasticity import Voigt_6_to_full_3x3_stress
from ...neighbours import find_indices_of_reversed_pairs, first_neighbours, neighbour_list, triplet_list
from ...numpy_tricks import mabincount


def _o(x, y, z=None, t=None):
    """Outer product"""
    if z is None and t is None:
        return x.reshape(-1, 3, 1) * y.reshape(-1, 1, 3)
    elif t is None:
        return x.reshape(-1, 3, 1, 1) * y.reshape(-1, 1, 3, 1) * z.reshape(-1, 1, 1, 3)
    else:
        return x.reshape(-1, 3, 1, 1, 1) * y.reshape(-1, 1, 3, 1, 1) * z.reshape(-1, 1, 1, 3, 1) \
               * t.reshape(-1, 1, 1, 1, 3)


class Manybody(Calculator):
    implemented_properties = ['free_energy', 'energy', 'stress', 'forces']
    default_parameters = {}
    name = 'Manybody'

    def __init__(self, atom_type, pair_type,
                 phi, d1phi, d2phi, d11phi, d12phi, d22phi,
                 theta, d1theta, d2theta, d3theta, d11theta, d12theta, d13theta, d22theta, d23theta, d33theta,
                 cutoff):
        Calculator.__init__(self)
        self.atom_type = atom_type
        self.pair_type = pair_type

        self.phi = phi
        self.d1phi = d1phi
        self.d2phi = d2phi
        self.d11phi = d11phi
        self.d12phi = d12phi
        self.d22phi = d22phi
        self.theta = theta
        self.d1theta = d1theta
        self.d2theta = d2theta
        self.d3theta = d3theta
        self.d11theta = d11theta
        self.d12theta = d12theta
        self.d13theta = d13theta
        self.d22theta = d22theta
        self.d23theta = d23theta
        self.d33theta = d33theta

        self.cutoff = cutoff

    def get_cutoff(self, atoms):
        if np.isscalar(self.cutoff):
            return self.cutoff

        # get internal atom types from atomic numbers
        elements = set(atoms.numbers)

        # loop over all possible element combinations
        cutoff = 0
        for i in elements:
            ii = self.atom_type(i)
            for j in elements:
                jj = self.atom_type(j)
                p = self.pair_type(ii, jj)
                cutoff = max(cutoff, self.cutoff[p])
        return cutoff

    def calculate(self, atoms, properties, system_changes):
        Calculator.calculate(self, atoms, properties, system_changes)

        # get internal atom types from atomic numbers
        t_n = self.atom_type(atoms.numbers)
        cutoff = self.get_cutoff(atoms)

        # construct neighbor list
        i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=cutoff)
        R_p = r_p * r_p

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        # construct triplet list
        first_n = first_neighbours(nb_atoms, i_p)
        ij_t, ik_t = triplet_list(first_n)
        rij_t = r_p[ij_t]
        Rij_t = rij_t * rij_t
        rik_t = r_p[ik_t]
        Rik_t = rik_t * rik_t
        Rjk_t = np.sum((r_pc[ik_t] - r_pc[ij_t]) ** 2, axis=1)
        rjk_t = np.sqrt(Rjk_t)

        # construct lists with atom and pair types
        ti_p = t_n[i_p]
        tij_p = self.pair_type(ti_p, t_n[j_p])
        ti_t = t_n[i_p[ij_t]]
        tij_t = self.pair_type(ti_t, t_n[j_p[ij_t]])
        tik_t = self.pair_type(ti_t, t_n[j_p[ik_t]])

        # potential-dependent functions
        theta_t = self.theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d1theta_t = self.d1theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d2theta_t = self.d2theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d3theta_t = self.d3theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)

        xi_p = np.bincount(ij_t, weights=theta_t, minlength=nb_pairs)

        phi_p = self.phi(R_p, r_p, xi_p, ti_p, tij_p)
        d1phi_p = self.d1phi(R_p, r_p, xi_p, ti_p, tij_p)
        d2phi_p = self.d2phi(R_p, r_p, xi_p, ti_p, tij_p)

        # calculate energy
        epot = 0.5 * np.sum(phi_p)

        # calculate forces (per pair)
        fij_pc = (d1phi_p * r_pc.T).T

        # calculate forces (per triplet)
        fij_tc = (d2phi_p[ij_t] * d1theta_t * r_pc[ij_t].T).T
        fik_tc = (d2phi_p[ij_t] * d2theta_t * r_pc[ik_t].T).T
        fjk_tc = (d2phi_p[ij_t] * d3theta_t * (r_pc[ik_t] - r_pc[ij_t]).T).T

        # Atomic forces (pair contribution)
        f_nc = mabincount(i_p, fij_pc, nb_atoms) - mabincount(j_p, fij_pc, nb_atoms)

        # Atomic forces (triplet contribution)
        f_nc += mabincount(i_p[ij_t], fij_tc, nb_atoms) - mabincount(j_p[ij_t], fij_tc, nb_atoms) \
                + mabincount(i_p[ik_t], fik_tc, nb_atoms) - mabincount(j_p[ik_t], fik_tc, nb_atoms) \
                + mabincount(j_p[ij_t], fjk_tc, nb_atoms) - mabincount(j_p[ik_t], fjk_tc, nb_atoms)

        # Virial (pair contribution)
        virial_v = np.array([
            r_pc.T[0] * fij_pc.T[0],  # xx
            r_pc.T[1] * fij_pc.T[1],  # yy
            r_pc.T[2] * fij_pc.T[2],  # zz
            r_pc.T[1] * fij_pc.T[2],  # xz
            r_pc.T[0] * fij_pc.T[2],  # yz
            r_pc.T[0] * fij_pc.T[1]   # xy
        ]).sum(axis=1)

        # Virial (triplet contribution)
        virial_v += np.array([
            r_pc[ij_t, 0] * fij_tc.T[0] + r_pc[ik_t, 0] * fik_tc.T[0] + (r_pc[ik_t, 0] - r_pc[ij_t, 0]) * fjk_tc.T[0],  # xx
            r_pc[ij_t, 1] * fij_tc.T[1] + r_pc[ik_t, 1] * fik_tc.T[1] + (r_pc[ik_t, 1] - r_pc[ij_t, 1]) * fjk_tc.T[1],  # yy
            r_pc[ij_t, 2] * fij_tc.T[2] + r_pc[ik_t, 2] * fik_tc.T[2] + (r_pc[ik_t, 2] - r_pc[ij_t, 2]) * fjk_tc.T[2],  # zz
            r_pc[ij_t, 1] * fij_tc.T[2] + r_pc[ik_t, 1] * fik_tc.T[2] + (r_pc[ik_t, 1] - r_pc[ij_t, 1]) * fjk_tc.T[2],  # xz
            r_pc[ij_t, 0] * fij_tc.T[2] + r_pc[ik_t, 0] * fik_tc.T[2] + (r_pc[ik_t, 0] - r_pc[ij_t, 0]) * fjk_tc.T[2],  # yz
            r_pc[ij_t, 0] * fij_tc.T[1] + r_pc[ik_t, 0] * fik_tc.T[1] + (r_pc[ik_t, 0] - r_pc[ij_t, 0]) * fjk_tc.T[1]   # xy
        ]).sum(axis=1)

        self.results = {'free_energy': epot,
                        'energy': epot,
                        'stress': virial_v / self.atoms.get_volume(),
                        'forces': f_nc}

    def get_hessian(self, atoms, format='sparse', divide_by_masses=False):
        """
        Calculate the Hessian matrix for a bond order potential. For an atomic
        configuration with N atoms in d dimensions the hessian matrix is a
        symmetric, hermitian matrix with a shape of (d*N,d*N). The matrix is
        in general a sparse matrix, which consists of dense blocks of
        shape (d,d), which are the mixed second derivatives.

        Parameters
        ----------
        atoms : ase.Atoms
            Atomic configuration in a local or global minima.
        format : "sparse" or "neighbour-list"
            Output format of the hessian matrix.
        divide_by_masses : bool
        	if true return the dynamic matrix else hessian matrix 

		Returns
		-------
		hessian : bsr_matrix
			either hessian or dynamic matrix

        Restrictions
        ------------
        This method is currently only implemented for three dimensional systems
        """
        if self.atoms is None:
            self.atoms = atoms

        # get internal atom types from atomic numbers
        t_n = self.atom_type(atoms.numbers)
        cutoff = self.get_cutoff(atoms)

        # construct neighbor list
        i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=2 * cutoff)

        mask_p = r_p > cutoff

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        # reverse pairs
        tr_p = find_indices_of_reversed_pairs(i_p, j_p, r_p)

        # normal vectors
        n_pc = (r_pc.T / r_p).T

        # construct triplet list
        first_n = first_neighbours(nb_atoms, i_p)
        ij_t, ik_t = triplet_list(first_n)
        rij_t = r_p[ij_t]
        Rij_t = rij_t * rij_t
        rik_t = r_p[ik_t]
        Rik_t = rik_t * rik_t
        Rjk_t = np.sum((r_pc[ik_t] - r_pc[ij_t]) ** 2, axis=1)
        rjk_t = np.sqrt(Rjk_t)

        # construct lists with atom and pair types
        ti_p = t_n[i_p]
        tij_p = self.pair_type(ti_p, t_n[j_p])
        ti_t = t_n[i_p[ij_t]]
        tij_t = self.pair_type(ti_t, t_n[j_p[ij_t]])
        tik_t = self.pair_type(ti_t, t_n[j_p[ik_t]])

        # potential-dependent functions
        theta_t = self.theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d1theta_t = self.d1theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d2theta_t = self.d2theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d3theta_t = self.d3theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d11theta_t = self.d11theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d22theta_t = self.d22theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d33theta_t = self.d33theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d12theta_t = self.d12theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d23theta_t = self.d23theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d13theta_t = self.d13theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)

        xi_p = np.bincount(ij_t, weights=theta_t, minlength=nb_pairs)

        d2phi_p = self.d2phi(R_p, r_p, xi_p, ti_p, tij_p)
        d11phi_p = self.d11phi(R_p, r_p, xi_p, ti_p, tij_p)
        d22phi_p = self.d22phi(R_p, r_p, xi_p, ti_p, tij_p)
        d12phi_p = self.d12phi(R_p, r_p, xi_p, ti_p, tij_p)

        # Term 1
        H_pcc =

        H_pcc += H_pcc.transpose(0, 2, 1)[tr_p]

        if format == "sparse":
            # Construct full diagonal terms from off-diagonal terms
            H_acc = np.zeros([nb_atoms, 3, 3])
            for x in range(3):
                for y in range(3):
                    H_acc[:, x, y] = -np.bincount(i_p, weights=H_pcc[:, x, y])

            if divide_by_masses:
                mass_nat = atoms.get_masses()
                geom_mean_mass_n = np.sqrt(mass_nat[i_p] * mass_nat[j_p])
                return \
                    bsr_matrix(((H_pcc.T / (2 * geom_mean_mass_n)).T, j_p, first_n), shape=(3 * nb_atoms, 3 * nb_atoms)) \
                    + bsr_matrix(((H_acc.T / (2 * mass_nat)).T, np.arange(nb_atoms), np.arange(nb_atoms + 1)),
                                 shape=(3 * nb_atoms, 3 * nb_atoms))
            else:
                return \
                    bsr_matrix((H_pcc / 2, j_p, first_n), shape=(3 * nb_atoms, 3 * nb_atoms)) \
                    + bsr_matrix((H_acc / 2, np.arange(nb_atoms), np.arange(nb_atoms + 1)),
                                 shape=(3 * nb_atoms, 3 * nb_atoms))

        # Neighbour list format
        elif format == "neighbour-list":
            return H_pcc / 2, i_p, j_p, r_pc, r_p

    def get_second_derivative(self, atoms, drda_pc, drdb_pc, i_p=None, j_p=None, r_p=None, r_pc=None):
        """
        Calculate the second derivative of the energy with respect to arbitrary variables a and b.

        Parameters
        ----------
        atoms: ase.Atoms
            Atomic configuration in a local or global minima.
        drda_pc/drdb_pc: array_like
            Derivative of atom positions with respect to variable a/b.
        i_p: array
            First atom index
        j_p: array
            Second atom index
        r_p: array
            Absolute distance 
        r_pc: array
            Distance vector
        """
        if self.atoms is None:
            self.atoms = atoms

        # get internal atom types from atomic numbers
        t_n = self.atom_type(atoms.numbers)
        cutoff = self.get_cutoff(atoms)

        if i_p is None or j_p is None or r_p is None or r_pc is None:
            # We need to construct the neighbor list ourselves
            i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=cutoff)

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        # normal vectors
        n_pc = (r_pc.T / r_p).T

        # derivative of the lengths of distance vectors
        drda_p = (n_pc * drda_pc).sum(axis=1)
        drdb_p = (n_pc * drdb_pc).sum(axis=1)

        # construct triplet list (we don't need jk_t here, hence neighbor to cutoff suffices)
        first_n = first_neighbours(nb_atoms, i_p)
        ij_t, ik_t, jk_t = triplet_list(first_n, r_p, cutoff, i_p, j_p)

        # construct lists with atom and pair types
        ti_p = t_n[i_p]
        tij_p = self.pair_type(ti_p, t_n[j_p])
        ti_t = t_n[i_p[ij_t]]
        tij_t = self.pair_type(ti_t, t_n[j_p[ij_t]])
        tik_t = self.pair_type(ti_t, t_n[j_p[ik_t]])

        # potential-dependent functions
        G_t = self.G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d1G_tc = self.d1G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d2G_tc = self.d2G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d11G_tcc = self.d11G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d12G_tcc = self.d12G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d22G_tcc = self.d22G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)

        xi_p = np.bincount(ij_t, weights=G_t, minlength=nb_pairs)

        d1F_p = self.d1F(r_p, xi_p, ti_p, tij_p)
        d2F_p = self.d2F(r_p, xi_p, ti_p, tij_p)
        d11F_p = self.d11F(r_p, xi_p, ti_p, tij_p)
        d12F_p = self.d12F(r_p, xi_p, ti_p, tij_p)
        d22F_p = self.d22F(r_p, xi_p, ti_p, tij_p)

        # Term 1
        T1 = (d11F_p * drda_p * drdb_p).sum()

        # Term 2
        T2 = (d12F_p[ij_t] * (d2G_tc * drda_pc[ik_t]).sum(axis=1) * drdb_p[ij_t]).sum()
        T2 += (d12F_p[ij_t] * (d2G_tc * drdb_pc[ik_t]).sum(axis=1) * drda_p[ij_t]).sum()
        T2 += (d12F_p[ij_t] * (d1G_tc * drda_pc[ij_t]).sum(axis=1) * drdb_p[ij_t]).sum()
        T2 += (d12F_p[ij_t] * (d1G_tc * drdb_pc[ij_t]).sum(axis=1) * drda_p[ij_t]).sum()

        # Term 3
        dxida_t = (d1G_tc * drda_pc[ij_t]).sum(axis=1) + (d2G_tc * drda_pc[ik_t]).sum(axis=1)
        dxidb_t = (d1G_tc * drdb_pc[ij_t]).sum(axis=1) + (d2G_tc * drdb_pc[ik_t]).sum(axis=1)
        T3 = (d22F_p *
              np.bincount(ij_t, weights=dxida_t, minlength=nb_pairs) *
              np.bincount(ij_t, weights=dxidb_t, minlength=nb_pairs)).sum()

        # Term 4
        Q_pcc = ((np.eye(3) - _o(n_pc, n_pc)).T / r_p).T

        T4 = (d1F_p * ((Q_pcc * drda_pc.reshape(-1, 3, 1)).sum(axis=1) * drdb_pc).sum(axis=1)).sum()

        # Term 5
        T5_t = ((d11G_tcc * drdb_pc[ij_t].reshape(-1, 3, 1)).sum(axis=1) * drda_pc[ij_t]).sum(axis=1)
        T5_t += ((drdb_pc[ik_t].reshape(-1, 1, 3) * d12G_tcc).sum(axis=2) * drda_pc[ij_t]).sum(axis=1)
        T5_t += ((drdb_pc[ij_t].reshape(-1, 3, 1) * d12G_tcc).sum(axis=1) * drda_pc[ik_t]).sum(axis=1)
        T5_t += ((d22G_tcc * drdb_pc[ik_t].reshape(-1, 3, 1)).sum(axis=1) * drda_pc[ik_t]).sum(axis=1)
        T5 = (d2F_p * np.bincount(ij_t, weights=T5_t, minlength=nb_pairs)).sum()

        return T1 + T2 + T3 + T4 + T5

    def get_non_affine_forces_from_second_derivative(self, atoms):
        """
        Compute the analytical non-affine forces.  

        Parameters
        ----------
        atoms: ase.Atoms
            Atomic configuration in a local or global minima.

        """

        if self.atoms is None:
            self.atoms = atoms

        i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=2 * self.get_cutoff(atoms))

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        naF_ncab = np.zeros((nb_atoms, 3, 3, 3))

        for m in range(0, nb_atoms):
            for cm in range(3):
                drdb_pc = np.zeros((nb_pairs, 3))
                drdb_pc[i_p == m, cm] = 1
                drdb_pc[j_p == m, cm] = -1
                for alpha in range(3):
                    for beta in range(3):
                        drda_pc = np.zeros((nb_pairs, 3))
                        drda_pc[:, alpha] = r_pc[:, beta]
                        naF_ncab[m, cm, alpha, beta] = \
                            self.get_second_derivative(atoms, drda_pc, drdb_pc, i_p=i_p, j_p=j_p, r_p=r_p, r_pc=r_pc)
        return naF_ncab / 2

    def get_born_elastic_constants(self, atoms):
        """
        Calculate the second derivative of the energy with respect to the Green-Lagrange strain.

        Parameters
        ----------
        atoms : ase.Atoms
            Atomic configuration in a local or global minima.

        Returns
        -------
        C : np.ndarray, shape (3, 3, 3, 3)
            Born elastic constants
        """
        if self.atoms is None:
            self.atoms = atoms

        # get internal atom types from atomic numbers
        t_n = self.atom_type(atoms.numbers)
        cutoff = self.get_cutoff(atoms)

        # construct neighbor list
        i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=cutoff)
        R_p = r_p * r_p

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        # construct triplet list (we don't need jk_t here, hence neighbor to cutoff suffices)
        first_n = first_neighbours(nb_atoms, i_p)
        ij_t, ik_t = triplet_list(first_n)
        rij_t = r_p[ij_t]
        rij_tc = r_pc[ij_t]
        Rij_t = rij_t * rij_t
        rik_t = r_p[ik_t]
        rik_tc = r_pc[ik_t]
        Rik_t = rik_t * rik_t
        rjk_tc = r_pc[ik_t] - r_pc[ij_t]
        Rjk_t = np.sum(rjk_tc * rjk_tc, axis=1)
        rjk_t = np.sqrt(Rjk_t)

        # construct lists with atom and pair types
        ti_p = t_n[i_p]
        tij_p = self.pair_type(ti_p, t_n[j_p])
        ti_t = t_n[i_p[ij_t]]
        tij_t = self.pair_type(ti_t, t_n[j_p[ij_t]])
        tik_t = self.pair_type(ti_t, t_n[j_p[ik_t]])

        # potential-dependent functions
        theta_t = self.theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d1theta_t = self.d1theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d2theta_t = self.d2theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d3theta_t = self.d3theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d11theta_t = self.d11theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d22theta_t = self.d22theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d33theta_t = self.d33theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d12theta_t = self.d12theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d23theta_t = self.d23theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)
        d13theta_t = self.d13theta(Rij_t, rij_t, Rik_t, rik_t, Rjk_t, rjk_t, ti_t, tij_t, tik_t)

        xi_p = np.bincount(ij_t, weights=theta_t, minlength=nb_pairs)

        d2phi_p = self.d2phi(R_p, r_p, xi_p, ti_p, tij_p)
        d11phi_p = self.d11phi(R_p, r_p, xi_p, ti_p, tij_p)
        d22phi_p = self.d22phi(R_p, r_p, xi_p, ti_p, tij_p)
        d12phi_p = self.d12phi(R_p, r_p, xi_p, ti_p, tij_p)

        # Term 1 disappears

        # Term 2
        C_abab = (d11phi_p * _o(r_pc, r_pc, r_pc, r_pc).T).T.sum(axis=0)

        # Term 3
        C_abab += (d2phi_p[ij_t] * (
                d11theta_t * _o(rij_tc, rij_tc, rij_tc, rij_tc).T
                + d12theta_t * (_o(rij_tc, rij_tc, rik_tc, rik_tc).T + _o(rik_tc, rik_tc, rij_tc, rij_tc).T)
                + d13theta_t * (_o(rij_tc, rij_tc, rjk_tc, rjk_tc).T + _o(rjk_tc, rjk_tc, rij_tc, rij_tc).T)
                + d22theta_t * _o(rik_tc, rik_tc, rik_tc, rik_tc).T
                + d23theta_t * (_o(rik_tc, rik_tc, rjk_tc, rjk_tc).T + _o(rjk_tc, rjk_tc, rik_tc, rik_tc).T)
                + d33theta_t * _o(rjk_tc, rjk_tc, rjk_tc, rjk_tc).T)).T.sum(axis=0)

        # Term 4
        C_abab += (d12phi_p[ij_t] * (
            2 * d1theta_t * (_o(rij_tc, rij_tc, rij_tc, rij_tc).T)
            + d2theta_t * (_o(rij_tc, rij_tc, rik_tc, rik_tc).T + _o(rik_tc, rik_tc, rij_tc, rij_tc).T)
            + d3theta_t * (_o(rij_tc, rij_tc, rjk_tc, rjk_tc).T + _o(rjk_tc, rjk_tc, rij_tc, rij_tc).T))).T.sum(axis=0)

        # Term 5
        tmpij_pcc = mabincount(ij_t, (d1theta_t * _o(rij_tc, rij_tc).T + d2theta_t * _o(rik_tc, rik_tc).T
                                      + d3theta_t * _o(rjk_tc, rjk_tc).T).T, minlength=nb_pairs)
        C_abab += (d22phi_p * tmpij_pcc.reshape(-1, 3, 3, 1, 1).T * tmpij_pcc.reshape(-1, 1, 1, 3, 3).T).T.sum(axis=0)

        return 2 * C_abab / atoms.get_volume()

    def get_stress_contribution_to_elastic_constants(self, atoms):
        """
        Compute the correction to the elastic constants due to non-zero stress in the configuration.
        Stress term  results from working with the Cauchy stress.


        Parameters
        ----------
        atoms: ase.Atoms
            Atomic configuration in a local or global minima.

        """

        stress_ab = Voigt_6_to_full_3x3_stress(atoms.get_stress())
        delta_ab = np.identity(3)

        # Term 1
        C1_abab = -stress_ab.reshape(3, 3, 1, 1) * delta_ab.reshape(1, 1, 3, 3)

        # Term 2
        C2_abab = (stress_ab.reshape(3, 1, 3, 1) * delta_ab.reshape(1, 3, 1, 3) \
                   + stress_ab.reshape(3, 1, 1, 3) * delta_ab.reshape(1, 3, 3, 1) \
                   + stress_ab.reshape(1, 3, 3, 1) * delta_ab.reshape(3, 1, 1, 3) \
                   + stress_ab.reshape(1, 3, 1, 3) * delta_ab.reshape(3, 1, 3, 1)) / 2

        return C1_abab + C2_abab

    def get_birch_coefficients(self, atoms):
        """
        Compute the Birch coefficients (Effective elastic constants at non-zero stress). 
        
        Parameters
        ----------
        atoms: ase.Atoms
            Atomic configuration in a local or global minima.

        """

        if self.atoms is None:
            self.atoms = atoms

        # Born (affine) elastic constants
        calculator = atoms.get_calculator()
        bornC_abab = calculator.get_born_elastic_constants(atoms)

        # Stress contribution to elastic constants
        stressC_abab = calculator.get_stress_contribution_to_elastic_constants(atoms)

        return bornC_abab + stressC_abab

    def get_non_affine_contribution_to_elastic_constants(self, atoms, eigenvalues=None, eigenvectors=None, tol=1e-5):
        """
        Compute the correction of non-affine displacements to the elasticity tensor.
        The computation of the occuring inverse of the Hessian matrix is bypassed by using a cg solver.

        If eigenvalues and and eigenvectors are given the inverse of the Hessian can be easily computed.


        Parameters
        ----------
        atoms: ase.Atoms
            Atomic configuration in a local or global minima.

        eigenvalues: array
            Eigenvalues in ascending order obtained by diagonalization of Hessian matrix.
            If given 

        eigenvectors: array
            Eigenvectors corresponding to eigenvalues.

        tol: float
            Tolerance for the conjugate-gradient solver. 

        """

        nat = len(atoms)

        calc = atoms.get_calculator()

        if (eigenvalues is not None) and (eigenvectors is not None):
            naforces_icab = calc.get_non_affine_forces(atoms)

            G_incc = (eigenvectors.T).reshape(-1, 3 * nat, 1, 1) * naforces_icab.reshape(1, 3 * nat, 3, 3)
            G_incc = (G_incc.T / np.sqrt(eigenvalues)).T
            G_icc = np.sum(G_incc, axis=1)
            C_abab = np.sum(G_icc.reshape(-1, 3, 3, 1, 1) * G_icc.reshape(-1, 1, 1, 3, 3), axis=0)

        else:
            H_nn = calc.get_hessian(atoms, "sparse")
            naforces_icab = calc.get_non_affine_forces(atoms)

            D_iab = np.zeros((3 * nat, 3, 3))
            for i in range(3):
                for j in range(3):
                    x, info = cg(H_nn, naforces_icab[:, :, i, j].flatten(), atol=tol)
                    if info != 0:
                        raise RuntimeError(
                            " info > 0: CG tolerance not achieved, info < 0: Exceeded number of iterations.")
                    D_iab[:, i, j] = x

            C_abab = np.sum(naforces_icab.reshape(3 * nat, 3, 3, 1, 1) * D_iab.reshape(3 * nat, 1, 1, 3, 3), axis=0)

        # Symmetrize 
        C_abab = (C_abab + C_abab.swapaxes(0, 1) + C_abab.swapaxes(2, 3) + C_abab.swapaxes(0, 1).swapaxes(2, 3)) / 4

        return -C_abab / atoms.get_volume()

    def get_non_affine_forces(self, atoms):
        if self.atoms is None:
            self.atoms = atoms

        # get internal atom types from atomic numbers
        t_n = self.atom_type(atoms.numbers)
        cutoff = self.get_cutoff(atoms)

        # construct neighbor list
        i_p, j_p, r_p, r_pc = neighbour_list('ijdD', atoms=atoms, cutoff=cutoff)

        nb_atoms = len(self.atoms)
        nb_pairs = len(i_p)

        # normal vectors
        n_pc = (r_pc.T / r_p).T
        dn_pcc = ((np.eye(3) - _o(n_pc, n_pc)).T / r_p).T

        # construct triplet list (we don't need jk_t here, hence neighbor to cutoff suffices)
        first_n = first_neighbours(nb_atoms, i_p)
        ij_t, ik_t, jk_t = triplet_list(first_n, r_p, cutoff, i_p, j_p)

        # construct lists with atom and pair types
        ti_p = t_n[i_p]
        tij_p = self.pair_type(ti_p, t_n[j_p])
        ti_t = t_n[i_p[ij_t]]
        tij_t = self.pair_type(ti_t, t_n[j_p[ij_t]])
        tik_t = self.pair_type(ti_t, t_n[j_p[ik_t]])

        # potential-dependent functions
        G_t = self.G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d1G_tc = self.d1G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d2G_tc = self.d2G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d11G_tcc = self.d11G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d12G_tcc = self.d12G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)
        d22G_tcc = self.d22G(r_pc[ij_t], r_pc[ik_t], ti_t, tij_t, tik_t)

        xi_p = np.bincount(ij_t, weights=G_t, minlength=nb_pairs)

        d1F_p = self.d1F(r_p, xi_p, ti_p, tij_p)
        d2F_p = self.d2F(r_p, xi_p, ti_p, tij_p)
        d11F_p = self.d11F(r_p, xi_p, ti_p, tij_p)
        d12F_p = self.d12F(r_p, xi_p, ti_p, tij_p)
        d22F_p = self.d22F(r_p, xi_p, ti_p, tij_p)

        # Derivative of xi with respect to the deformation gradient
        dxidF_pab = mabincount(ij_t, _o(d1G_tc, r_pc[ij_t]) + _o(d2G_tc, r_pc[ik_t]), minlength=nb_pairs)

        # Term 1
        naF1_ncab = d11F_p.reshape(-1, 1, 1, 1) * _o(n_pc, n_pc, r_pc)

        # Term 2
        naF21_tcab = (d12F_p[ij_t] * (_o(n_pc[ij_t], d1G_tc, r_pc[ij_t])
                                      + _o(n_pc[ij_t], d2G_tc, r_pc[ik_t])
                                      + _o(d1G_tc, n_pc[ij_t], r_pc[ij_t])
                                      + _o(d2G_tc, n_pc[ij_t], r_pc[ij_t])).T).T

        naF22_tcab = -(d12F_p[ij_t] * (_o(n_pc[ij_t], d1G_tc, r_pc[ij_t])
                                       + _o(n_pc[ij_t], d2G_tc, r_pc[ik_t])
                                       + _o(d1G_tc, n_pc[ij_t], r_pc[ij_t])).T).T

        naF23_tcab = -(d12F_p[ij_t] * (_o(d2G_tc, n_pc[ij_t], r_pc[ij_t])).T).T

        # Term 3
        naF31_tcab = \
            d22F_p[ij_t].reshape(-1, 1, 1, 1) * d1G_tc.reshape(-1, 3, 1, 1) * dxidF_pab[ij_t].reshape(-1, 1, 3, 3)
        naF32_tcab = \
            d22F_p[ij_t].reshape(-1, 1, 1, 1) * d2G_tc.reshape(-1, 3, 1, 1) * dxidF_pab[ij_t].reshape(-1, 1, 3, 3)

        # Term 4
        naF4_ncab = (d1F_p * (dn_pcc.reshape(-1, 3, 3, 1) * r_pc.reshape(-1, 1, 1, 3)).T).T

        # Term 5
        naF51_tcab = (d2F_p[ij_t] * (
                d11G_tcc.reshape(-1, 3, 3, 1) * r_pc[ij_t].reshape(-1, 1, 1, 3)
                + d12G_tcc.reshape(-1, 3, 3, 1) * r_pc[ik_t].reshape(-1, 1, 1, 3)
                + d22G_tcc.reshape(-1, 3, 3, 1) * r_pc[ik_t].reshape(-1, 1, 1, 3)
                + (d12G_tcc.reshape(-1, 3, 3, 1)).swapaxes(1, 2) * r_pc[ij_t].reshape(-1, 1, 1, 3)).T).T

        naF52_tcab = -(d2F_p[ij_t] * (
                d11G_tcc.reshape(-1, 3, 3, 1) * r_pc[ij_t].reshape(-1, 1, 1, 3)
                + d12G_tcc.reshape(-1, 3, 3, 1) * r_pc[ik_t].reshape(-1, 1, 1, 3)).T).T

        naF53_tcab = -(d2F_p[ij_t] * (
                d12G_tcc.reshape(-1, 3, 3, 1).swapaxes(1, 2) * r_pc[ij_t].reshape(-1, 1, 1, 3)
                + d22G_tcc.reshape(-1, 3, 3, 1) * r_pc[ik_t].reshape(-1, 1, 1, 3)).T).T

        naforces_icab = \
            mabincount(i_p, naF1_ncab, minlength=nb_atoms) \
            - mabincount(j_p, naF1_ncab, minlength=nb_atoms) \
            + mabincount(i_p[ij_t], naF21_tcab, minlength=nb_atoms) \
            + mabincount(j_p[ij_t], naF22_tcab, minlength=nb_atoms) \
            + mabincount(j_p[ik_t], naF23_tcab, minlength=nb_atoms) \
            + mabincount(i_p[ij_t], naF31_tcab, minlength=nb_atoms) \
            - mabincount(j_p[ij_t], naF31_tcab, minlength=nb_atoms) \
            + mabincount(i_p[ij_t], naF32_tcab, minlength=nb_atoms) \
            - mabincount(j_p[ik_t], naF32_tcab, minlength=nb_atoms) \
            + mabincount(i_p, naF4_ncab, minlength=nb_atoms) \
            - mabincount(j_p, naF4_ncab, minlength=nb_atoms) \
            + mabincount(i_p[ij_t], naF51_tcab, minlength=nb_atoms) \
            + mabincount(j_p[ij_t], naF52_tcab, minlength=nb_atoms) \
            + mabincount(j_p[ik_t], naF53_tcab, minlength=nb_atoms)

        return naforces_icab / 2
