import os
from moviepy.editor import ImageSequenceClip
import numpy as np
import collections
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from skimage.transform import rescale
from functools import reduce
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import tqdm
import math
from copy import deepcopy

def compose_functions(fseq):
    def compose(f1, f2):
        return lambda x: f2(f1(x))
    return reduce(compose, fseq, lambda _: _)

def copy_state_to_model(archive_params, model):
    size_map = [
        'number of out channels',
        'number of in channels',
        'kernel x dimension',
        'kernel y dimension'
    ]

    model_params = dict(model.named_parameters())
    archive_keys = archive_params.keys()
    model_keys = sorted(model_params.keys())
    archive_keys = sorted([k for k in archive_keys if 'seq' not in k])

    approx = 0
    skipped = 0
    for key in archive_keys:
        if key not in model_keys:
            print('[WARNING]   Key ' + key + ' present in archive but not in model; skipping.')
            skipped += 1
            continue

        min_size = [min(mdim,adim) for mdim, adim in zip(list(model_params[key].size()), list(archive_params[key].size()))]
        msize, asize = model_params[key].size(), archive_params[key].size()
        if msize != asize:
            approx += 1
            wrong_dim = -1
            for dim in range(len(msize)):
                if msize[dim] != asize[dim]:
                    wrong_dim = dim
                    break
            print('[WARNING]   ' + key + ' has different ' + size_map[wrong_dim] + ' in model and archive: ' + str(model_params[key].size()) + ', ' + str(archive_params[key].size()))
            varchive = torch.std(archive_params[key])
            vmodel = torch.std(model_params[key])
            model_params[key].data -= torch.mean(model_params[key].data)
            model_params[key].data *= ((varchive / 5) / vmodel).data[0]
            model_params[key].data += torch.mean(archive_params[key])

        min_size_slices = tuple([slice(*(s,)) for s in min_size])
        model_params[key].data[min_size_slices] = archive_params[key][min_size_slices]
        
        if 'enc' not in key and msize != asize and wrong_dim == 1:
            fm_count = asize[1]/2
            chunks = (msize[1]-fm_count)/(asize[1]-fm_count)
            for i in range(1,chunks+1):
                model_params[key].data[:,i*fm_count:(i+1)*fm_count] = archive_params[key][:,fm_count:] / (i+1)
            model_params[key].data[:,fm_count:] /= sum([1.0/k for k in range(2,chunks+2)])
            means = torch.zeros(model_params[key].data[:,fm_count:].size())
            std = varchive/5
            model_params[key].data[:,fm_count:] += torch.normal(means, std).cuda()
    print('Copied ' + str(len(model_keys) - approx) + ' parameters exactly, ' + str(approx) + ' parameters partially. Skipped ' + str(skipped) + ' parameters.')
        
def get_colors(angles, f, c):
    colors = f(angles)
    colors = c(colors)
    return colors

def dv(vfield, name=None, downsample=0.5):
    dim = vfield.shape[-2]
    assert type(vfield) == np.ndarray

    lengths = np.squeeze(np.sqrt(vfield[:,:,:,0] ** 2 + vfield[:,:,:,1] ** 2))
    lengths = (lengths - np.min(lengths)) / (np.max(lengths) - np.min(lengths))
    angles = np.squeeze(np.angle(vfield[:,:,:,0] + vfield[:,:,:,1]*1j))

    angles = (angles - np.min(angles)) / (np.max(angles) - np.min(angles)) * np.pi
    angles -= np.pi/8
    angles[angles<0] += np.pi
    off_angles = angles + np.pi/4
    off_angles[off_angles>np.pi] -= np.pi

    scolors = get_colors(angles, f=lambda x: np.sin(x) ** 1.4, c=cm.viridis)
    ccolors = get_colors(off_angles, f=lambda x: np.sin(x) ** 1.4, c=cm.magma)

    # mix
    scolors[:,:,0] = ccolors[:,:,0]
    scolors[:,:,1] = (ccolors[:,:,1] + scolors[:,:,1]) / 2
    scolors = scolors[:,:,:-1] #
    scolors = 1 - (1 - scolors) * lengths.reshape((dim, dim, 1)) ** .8 #

    img = np_upsample(scolors, downsample) if downsample is not None else scolors

    if name is not None:
        plt.imsave(name + '.png', img)
    else:
        return img

def np_upsample(img, factor):
    if factor == 1:
        return img

    if img.ndim == 2:
        return rescale(img, factor)
    elif img.ndim == 3:
        b = np.empty((int(img.shape[0] * factor), int(img.shape[1] * factor), img.shape[2]))
        for idx in range(img.shape[2]):
            b[:,:,idx] = np_upsample(img[:,:,idx], factor)
        return b
    else:
        assert False

def np_downsample(img, factor):
    data_4d = np.expand_dims(img, axis=1)
    result = nn.AvgPool2d(factor)(torch.from_numpy(data_4d))
    return result.numpy()[:, 0, :, :]

def center_field(field):
    wrap = type(field) == np.ndarray
    if wrap:
        field = [field]
    for idx, vfield in enumerate(field):
        vfield[:,:,:,0] = vfield[:,:,:,0] - np.mean(vfield[:,:,:,0])
        vfield[:,:,:,1] = vfield[:,:,:,1] - np.mean(vfield[:,:,:,1])
        field[idx] = vfield
    return field[0] if wrap else field

def display_v(vfield, name=None, center=False):
    if center:
        center_field(vfield)

    if type(vfield) == list:
        dim = max([vf.shape[-2] for vf in vfield])
        vlist = [np.expand_dims(np_upsample(vf[0], dim/vf.shape[-2]), axis=0) for vf in vfield]
        for idx, _ in enumerate(vlist[1:]):
            vlist[idx+1] += vlist[idx]
        imgs = [dv(vf) for vf in vlist]
        gif(name, np.stack(imgs) * 255)
    else:
        assert (name is not None)
        dv(vfield, name)

def dvl(V_pred, name, mag=10):
    factor = V_pred.shape[1] // 100
    if factor > 1:
        V_pred = V_pred[:,::factor,::factor,:]
    V_pred *= 10
    plt.figure(figsize=(6,6))
    X, Y = np.meshgrid(np.arange(-1, 1, 2.0/V_pred.shape[-2]), np.arange(-1, 1, 2.0/V_pred.shape[-2]))
    U, V = np.squeeze(np.vsplit(np.swapaxes(V_pred,0,-1),2))
    colors = np.arctan2(U,V)   # true angle
    plt.title('V_pred')
    plt.gca().invert_yaxis()
    Q = plt.quiver(X, Y, U, V, colors, scale=6, width=0.002, angles='uv', pivot='tail')
    qk = plt.quiverkey(Q, 10.0, 10.0, 2, r'$2 \frac{m}{s}$', labelpos='E', \
                       coordinates='figure')

    plt.savefig(name + '.png')
    plt.clf()

def reverse_dim(var, dim):
    if var is None:
        return var
    idx = range(var.size()[dim] - 1, -1, -1)
    idx = torch.LongTensor(idx)
    if var.is_cuda:
        idx = idx.cuda()
    return var.index_select(dim, idx)

def reduce_seq(seq, f):
    size = min([x.size()[-1] for x in seq])
    return f([center(var, (-2,-1), var.size()[-1] - size) for var in seq], 1)

def center(var, dims, d):
    if not isinstance(d, collections.Sequence):
        d = [d for i in range(len(dims))]
    for idx, dim in enumerate(dims):
        if d[idx] == 0:
            continue
        var = var.narrow(dim, d[idx]/2, var.size()[dim] - d[idx])
    return var

def crop(data_2d, crop):
    return data_2d[crop:-crop,crop:-crop]

def save_chunk(chunk, name, norm=True):
    if type(chunk) != np.ndarray:
        try:
            if chunk.is_cuda:
                chunk = chunk.data.cpu().numpy()
            else:
                chunk = chunk.data.numpy()
        except Exception as e:
            if chunk.is_cuda:
                chunk = chunk.cpu().numpy()
            else:
                chunk = chunk.numpy()
    chunk = np.squeeze(chunk).astype(np.float64)
    if norm:
        chunk[:50,:50] = 0
        chunk[:10,:10] = 1
        chunk[-50:,-50:] = 1
        chunk[-10:,-10:] = 0
    plt.imsave(name + '.png', 1 - chunk, cmap='Greys')
        
def gif(filename, array, fps=8, scale=1.0, norm=False):
    """Creates a gif given a stack of images using moviepy
    >>> X = randn(100, 64, 64)
    >>> gif('test.gif', X)
    Parameters
    ----------
    filename : string
        The filename of the gif to write to
    array : array_like
        A numpy array that contains a sequence of images
    fps : int
        frames per second (default: 10)
    scale : float
        how much to rescale each image by (default: 1.0)
    """
    tqdm.pos = 0  # workaround for tqdm bug when using it in multithreading

    array = (array - np.min(array)) / (np.max(array) - np.min(array))
    array *= 255
    # ensure that the file has the .gif extension
    fname, _ = os.path.splitext(filename)
    filename = fname + '.gif'

    # copy into the color dimension if the images are black and white
    if array.ndim == 3:
        array = array[..., np.newaxis] * np.ones(3)

    if norm and array.shape[1] > 1000:
        # add 'signature' block to top left and bottom right
        array[:,:50,:50] = 0
        array[:,:10,:10] = 255
        array[:,-50:,-50:] = 255
        array[:,-10:,-10:] = 0

    # make the moviepy clip
    clip = ImageSequenceClip(list(array), fps=fps).resize(scale)
    clip.write_gif(filename, fps=fps, verbose=False)
    return clip

def downsample(x):
    if x > 0:
        return nn.AvgPool2d(2**x, count_include_pad=False)
    else:
        return (lambda y: y)

def upsample(x):
    if x > 0:
        return nn.Upsample(scale_factor=2**x, mode='bilinear')
    else:
        return (lambda y: y)

def gridsample(source, field, padding_mode):
    """
    A version of the PyTorch grid sampler that uses size-agnostic conventions.
    Vectors with values -1 or +1 point to the actual edges of the images
    (as opposed to the centers of the border pixels as in PyTorch 4.1).

    `source` and `field` should be PyTorch tensors on the same GPU, with
    `source` arranged as a PyTorch image, and `field` as a PyTorch vector field.

    `padding_mode` is required because it is a significant consideration.
    It determines the value sampled when a vector is outside the range [-1,1]
    Options are:
     - "zero" : produce the value zero (okay for sampling images with zero as
                background, but potentially problematic for sampling masks and
                terrible for sampling from other vector fields)
     - "border" : produces the value at the nearest inbounds pixel (great for
                  masks and residual fields)

    If sampling a field (ie. `source` is a vector field), best practice is to
    subtract out the identity field from `source` first (if present) to get a
    residual field.
    Then sample it with `padding_mode = "border"`.
    This should behave as if source was extended as a uniform vector field
    beyond each of its boundaries.
    Note that to sample from a field, the source field must be rearranged to
    fit the conventions for image dimensions in PyTorch. This can be done by
    calling `source.permute(0,3,1,2)` before passing to `gridsample()` and
    `result.permute(0,2,3,1)` to restore the result.
    """
    if source.shape[2] != source.shape[3]:
        raise NotImplementedError('Grid sampling from non-square tensors '
                                  'not yet implementd here.')
    scaled_field = field * source.shape[2] / (source.shape[2] - 1)
    return F.grid_sample(source, scaled_field, mode="bilinear", padding_mode=padding_mode)

def gridsample_residual(source, residual, padding_mode):
    """
    Similar to `gridsample()`, but takes a residual field.
    This abstracts away generation of the appropriate identity grid.
    """
    field = residual + identity_grid(residual.shape, device=residual.device)
    return gridsample(source, field, padding_mode)

def compose(U, V):
  """Compose two vector fields, U(V(x))
  """
  return U + gridsample_residual(V.permute(0,3,1,2), U, 'border').permute(0,2,3,1) 

def _create_identity_grid(size):
    with torch.no_grad():
        id_theta = torch.Tensor([[[1,0,0],[0,1,0]]]) # identity affine transform
        I = F.affine_grid(id_theta,torch.Size((1,1,size,size)))
        I *= (size - 1) / size # rescale the identity provided by PyTorch
        return I

def identity_grid(size, cache=False, device=None):
    """
    Returns a size-agnostic identity field with -1 and +1 pointing to the
    corners of the image (not the centers of the border pixels as in
    PyTorch 4.1).

    Use `cache = True` to cache the identity for faster recall.
    This can speed up recall, but may be a burden on cpu/gpu memory.

    `size` can be either an `int` or a `torch.Size` of the form
    `(N, C, H, W)`. `H` and `W` must be the same (a square tensor).
    `N` and `C` are ignored.
    """
    if isinstance(size,torch.Size):
        if (size[2] == size[3] # image
            or (size[3] == 2 and size[1] == size[2])): # field
            size = size[2]
        else:
            raise ValueError("Bad size: {}. Expected a square tensor size.".format(size))
    if device is None:
        device = torch.cuda.current_device()
    if size in identity_grid._identities:
        return identity_grid._identities[size].to(device)
    I = _create_identity_grid(size)
    if cache:
        identity_grid._identities[size] = I
    return I.to(device)
identity_grid._identities = {}

def rel_to_grid_px(u, N):
  return N*(u + 1) / 2 - 0.5

def rel_to_grid(U):
  """Convert a relative vector field [-1,+1] to a vector field in image grid coords

  Vector convention:
   A vector of -1,-1 points to the upper left corner of the image, which maps to
   -0.5,-0.5 in the image grid coordinates.
   A vector of +1,+1 points to the lower right corner of the image, which maps to
   N-0.5, N-0.5 in the image grid coordinates.
 
  Args
    U: 4D tensor in vector field convention (1xXxYx2), where vectors are stored as
       residuals in relative convention [-1,+1]
  
  Returns
    V: 4D tensor in vector field convention (1xXxYx2), where vectors are stored as
       residuals in image grid coordinates [0, N-1]
  """
  V = deepcopy(U)
  N = V.shape[1]
  M = V.shape[2]
  V[:,:,:,0] = rel_to_grid_px(V[:,:,:,0], N) 
  V[:,:,:,1] = rel_to_grid_px(V[:,:,:,1], M)
  return V 

def grid_to_rel_px(v, N):
  return 2*(v + 0.5) / N - 1 

def grid_to_rel(U):
  """Convert a vector field in image grid coordinates to a relative vector field

  Vector convention:
   See rel_to_grid
 
  Args
    U: 4D tensor in vector field convention (1xXxYx2), where vectors are stored as
       residuals in image grid coordinates [0, N-1]
  
  Returns
    V: 4D tensor in vector field convention (1xXxYx2), where vectors are stored as
       residuals in relative coordinates [-1, +1]
  """
  V = deepcopy(U)
  N = V.shape[1]
  M = V.shape[2]
  V[:,:,:,0] = grid_to_rel_px(V[:,:,:,0], N) 
  V[:,:,:,1] = grid_to_rel_px(V[:,:,:,1], M) 
  return V
 
def invert_bruteforce(U):
  """Compute the inverse vector field of residual field U

  This function leverages existing pytorch functions. A faster implementation could
  be written with CUDA.

  This inverse computes the bilinearly weighted sum of all vectors for a given source
  pixel, such that

  ```
  V(s) = \frac{- \sum_{r} w_r U(r)} {\sum_{r} w_r}  \{r | r + U(r) - s \le 1\} \\
  w_r = | r + U(r) - s |
  ```

  Args
     U: 4D tensor in vector field convention (1xXxYx2), where vectors are stored
        as absolute residuals.

  Returns
     V: 4D tensor for absolute residual vector field such that V(U) = I. Undefined
        locations in U will be filled with the average of its neighbors.
  """
  n = U.shape[1] 
  m = U.shape[2]
  N = torch.zeros_like(U)
  D = torch.zeros_like(U)
  for ri in range(U.shape[1]):
    for rj in range(U.shape[2]):
      uj, ui = U[0, ri, rj, :]
      ui, uj = ui.item(), uj.item()
      _si = rel_to_grid_px(grid_to_rel_px(ri, n) + ui, n)
      _sj = rel_to_grid_px(grid_to_rel_px(rj, m) + uj, m)
      for z in range(4):
        if z == 0:
          si, sj = math.floor(_si), math.floor(_sj)
        elif z == 1:
          si, sj = math.floor(_si), math.floor(_sj+1)
        elif z == 2:
          si, sj = math.floor(_si+1), math.floor(_sj+1)
        else:
          si, sj = math.floor(_si+1), math.floor(_sj)
        # print('  (ri, rj): {0}'.format((ri, rj)))
        # print('  (_si, si): {0}'.format((_si, si)))
        # print('  (_sj, sj): {0}'.format((_sj, sj)))
        if (si < U.shape[1]) & (si >= 0): 
          if (sj < U.shape[2]) & (sj >= 0): 
            w = (1 - abs(_si - si)) * (1 - abs(_sj - sj))
            # print('{0} = {1} * {2}'.format((si, sj), round(w,2), U[0, ri, rj, :]))
            N[0, si, sj, :] -= w * U[0, ri, rj, :]
            D[0, si, sj, :] += w
  # nan_mask = torch.iszero(D)
  V = torch.div(N, D)
  return V

def tensor_approx_eq(A, B, eta=1e-7):
  return torch.all(torch.lt(torch.abs(torch.add(A, -B)), eta))

def invert(U, lr=0.1, max_iter=1000, currn=5, avgn=20, eps=1e-9):
  """Compute the inverse vector field of residual field U by optimization

  This method uses the following loss function:
  ```
  L = \frac{1}{2} \| U(V) - I \|^2 + \frac{1}{2} \| V(U) - I \|^2
  ```

  Args
     U: 4D tensor in vector field convention (1xXxYx2), where vectors are stored
        as absolute residuals.

  Returns
     V: 4D tensor for absolute residual vector field such that V(U) = I.
  """
  V = -deepcopy(U) 
  if tensor_approx_eq(U,V):
    return V 
  V.requires_grad = True
  n = U.shape[1] * U.shape[2]
  opt = torch.optim.SGD([V], lr=lr)
  costs = []
  currt = 0
  print('Optimizing inverse field')
  for t in range(max_iter):
    currt = t
    f = compose(U, V) 
    g = compose(V, U)
    L = 0.5*torch.mean(f**2) + 0.5*torch.mean(g**2)
    costs.append(L)
    L.backward()
    V.grad *= n
    opt.step()
    opt.zero_grad()
    assert(not torch.isnan(costs[-1]))
    if costs[-1] == 0:
      break
    if len(costs) > avgn + currn:
        hist = sum(costs[-(avgn+currn):-currn]).item() / avgn
        curr = sum(costs[-currn:]).item() / currn
        if abs((hist-curr)/hist) < eps:
            break
  V.requires_grad = False
  print('Final cost @ t={0}: {1}'.format(currt, costs[-1].item()))
  return V
