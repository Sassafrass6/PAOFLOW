# 
# PAOFLOW
#
# Utility to construct and operate on Hamiltonians from the Projections of DFT wfc on Atomic Orbital bases (PAO)
#
# Copyright (C) 2016-2018 ERMES group (http://ermes.unt.edu, mbn@unt.edu)
#
# Reference:
# M. Buongiorno Nardelli, F. T. Cerasoli, M. Costa, S Curtarolo,R. De Gennaro, M. Fornari, L. Liyanage, A. Supka and H. Wang,
# PAOFLOW: A utility to construct and operate on ab initio Hamiltonians from the Projections of electronic wavefunctions on
# Atomic Orbital bases, including characterization of topological materials, Comp. Mat. Sci. vol. 143, 462 (2018).
#
# This file is distributed under the terms of the
# GNU General Public License. See the file `License'
# in the root directory of the present distribution,
# or http://www.gnu.org/copyleft/gpl.txt .
#

import numpy as np
from scipy import signal
from mpi4py import MPI

comm = MPI.COMM_WORLD
rank = comm.Get_rank()

def do_Boltz_tensors ( data_controller, smearing, temp, ene, velkp, ispin, channels, weights):
  # Compute the L_alpha tensors for Boltzmann transport

  arrays,attributes = data_controller.data_dicts()

  esize = ene.size
  arrays['scattering_tau'] = get_tau(data_controller, temp, channels, weights)

#### Forced t_tensor to have all components
  t_tensor = np.array([[0,0],[1,1],[2,2],[0,1],[0,2],[1,2]], dtype=int)

  # Quick call function for L_loop (None is smearing type)
  fLloop = lambda spol : L_loop(data_controller, temp, smearing, ene, velkp, t_tensor, spol, ispin)

  # Quick call function for Zeros on rank Zero
  zoz = lambda r: (np.zeros((3,3,esize), dtype=float) if r==0 else None)

  L0 = zoz(rank)
  L0aux = fLloop(0)
  comm.Reduce(L0aux, L0, op=MPI.SUM)
  L0aux = None

  if rank == 0:
    # Assign lower triangular to upper triangular
    sym = lambda L : (L[0,1], L[0,2], L[1,2])
    L0[1,0],L0[2,0],L0[2,1] = sym(L0)

  L1 = zoz(rank)
  L1aux = fLloop(1)
  comm.Reduce(L1aux, L1, op=MPI.SUM)
  L1aux = None

  L2 = zoz(rank)
  L2aux = fLloop(2)
  comm.Reduce(L2aux, L2, op=MPI.SUM)
  L2aux = None

  if rank == 0:
    L1[1,0],L1[2,0],L1[2,1] = sym(L1)
    L2[1,0],L2[2,0],L2[2,1] = sym(L2)

  return (L0, L1, L2) if rank==0 else (None, None, None)


def get_tau ( data_controller, temp, channels, weights ):
  import numpy as np
  import scipy.constants as cp
  from .TauModel import TauModel
  from .do_tau_models import builtin_tau_model

  arry,attr = data_controller.data_dicts()
  snktot = arry['E_k'].shape[0]

  taus = []
  for c in channels:
    if c == 'acoustic':
      a_tau = np.ones((snktot), dtype=float)
      taus.append(a_tau)

  bnd = attr['bnd']
  eigs = np.abs(arry['E_k'][:,:bnd,:])
  snktot,_,nspin = eigs.shape

  models = []
  if channels != None:
    if len(weights) == 0:
      weights = np.ones(len(channels))
    elif len(weights) != len(models):
      raise Exception('Length of weights does not match the number of channels.')
    for i,c in enumerate(channels):
      if isinstance(c,str):
        models.append(builtin_tau_model(c,attr['tau_dict'],weights[i]))
      elif isinstance(c,TauModel):
        c.weight = weights[i]
        models.append(c)
      else:
        print('Invalid channel type.')

  if len(models) == 0:
    # Constant relaxation time approximation with tau = 1
    tau = np.ones((snktot,bnd,nspin), dtype=float)

  else:
    # Compute tau as a harmonic sum of scattering channel contributions.
    tau = np.zeros((snktot,bnd,nspin), dtype=float)
    for m in models:
      try:
        tau += m.weight/m.evaluate(temp, eigs)
      except KeyError as e:
        from .report_exception import report_exception
        print('Ensure that all required parameters are specified in the provided dictionary.')
        report_exception()
        raise e
    tau = 1/tau

  return tau



def L_loop ( data_controller, temp, smearing, ene, velkp, t_tensor, alpha, ispin ):
  from .smearing import gaussian,metpax
  # We assume tau=1 in the constant relaxation time approximation

  arrays,attributes = data_controller.data_dicts()

  esize = ene.size

  snktot = arrays['E_k'].shape[0]

  bnd = attributes['bnd']
  kq_wght = 1./attributes['nkpnts']
  if smearing is not None and smearing != 'gauss' and smearing != 'm-p':
    print('%s Smearing Not Implemented.'%smearing)
    comm.Abort()

  L = np.zeros((3,3,esize), dtype=float)

  for n in range(bnd):
    Eaux = np.reshape(np.repeat(arrays['E_k'][:,n,ispin],esize), (snktot,esize))
    delk = (np.reshape(np.repeat(arrays['deltakp'][:,n,ispin],esize), (snktot,esize)) if smearing!=None else None)
    EtoAlpha = np.power(Eaux[:,:]-ene, alpha)
    if smearing is None:
      Eaux -= ene
      smearA = 1/(4*temp*(np.cosh(Eaux/(2*temp))**2))
    else:
      if smearing == 'gauss':
        smearA = gaussian(Eaux, ene, delk)
      elif smearing == 'm-p':
        smearA = metpax(Eaux, ene, delk)
    for l in range(t_tensor.shape[0]):
      i = t_tensor[l][0]
      j = t_tensor[l][1]
      L[i,j,:] += np.sum(kq_wght*arrays['scattering_tau'][:,n,ispin]*velkp[:,i,n,ispin]*velkp[:,j,n,ispin]*(smearA*EtoAlpha).T, axis=1)
  '''
  # noise reduction using a running average (correlation function)
  # Only possible for sigma vs chemical potential
  win = int(esize*0.025)
  N = esize
  for l in range(t_tensor.shape[0]):
    i = t_tensor[l][0]
    j = t_tensor[l][1]
    L[i,j,:] = signal.correlate(L[i,j,:] , np.ones(win), mode='same', method='fft')/win
  '''
  return L
