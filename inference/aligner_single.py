from process import Process
from cloudvolume import CloudVolume as cv
from cloudvolume.lib import Vec
import torch
import numpy as np
import os
import json
import math
from time import time
from copy import deepcopy, copy
from helpers import save_chunk, crop, upsample, gridsample_residual, np_downsample
import scipy
import scipy.ndimage
import h5py

from skimage.morphology import disk as skdisk
from skimage.filters.rank import maximum as skmaximum

from boundingbox import BoundingBox

from pathos.multiprocessing import ProcessPool, ThreadPool
from threading import Lock

import torch.nn as nn

class Aligner:
  def __init__(self, model_path, max_displacement, crop,
               mip_range, high_mip_chunk, src_ng_path, dst_ng_path,
               render_low_mip=2, render_high_mip=6, is_Xmas=False, threads=3,
               max_chunk=(1024, 1024), max_render_chunk=(2048*2, 2048*2),
               skip=0, topskip=0, size=7, should_contrast=True, num_targets=1,
               flip_average=True, run_pairs=False, write_intermediaries=False,
               upsample_residuals=False, old_upsample=False, old_vectors=False):
    self.process_high_mip = mip_range[1]
    self.process_low_mip  = mip_range[0]
    self.render_low_mip   = render_low_mip
    self.render_high_mip  = render_high_mip
    self.high_mip         = max(self.render_high_mip, self.process_high_mip)
    self.high_mip_chunk   = high_mip_chunk
    self.max_chunk        = max_chunk
    self.max_render_chunk = max_render_chunk
    self.num_targets      = num_targets
    self.run_pairs = run_pairs
    self.size = size
    self.old_vectors=old_vectors

    self.max_displacement = max_displacement
    self.crop_amount      = crop
    self.org_ng_path      = src_ng_path
    self.src_ng_path      = self.org_ng_path

    self.dst_ng_path = os.path.join(dst_ng_path, 'image')
    self.tmp_ng_path = os.path.join(dst_ng_path, 'intermediate')
    if (self.run_pairs): 
        self.field_sf_ng_path = os.path.join(dst_ng_path, 'field_sf')
        #self.gauss_filter = self.Gaussian_filter(1025, 256) 
        #self.image_pixels_sum = np.empty(1)
        #self.field_sf_sum = np.empty((1,1))
        #self.reg_field = np.zeros(2, dtype=np.float32)
        #self.x_len = 0
        #self.y_len = 0
    else: 
        self.field_sf_ng_path = ""

    self.enc_ng_paths   = [os.path.join(dst_ng_path, 'enc/{}'.format(i))
                                                     for i in range(self.process_high_mip + 10)] #TODO

    self.res_ng_paths   = [os.path.join(dst_ng_path, 'vec/{}'.format(i))
                                                    for i in range(self.process_high_mip + 10)] #TODO
    self.x_res_ng_paths = [os.path.join(r, 'x') for r in self.res_ng_paths]
    self.y_res_ng_paths = [os.path.join(r, 'y') for r in self.res_ng_paths]

    self.cumres_ng_paths   = [os.path.join(dst_ng_path, 'cumulative_vec/{}'.format(i))
                                                    for i in range(self.process_high_mip + 10)] #TODO
    self.x_cumres_ng_paths = [os.path.join(r, 'x') for r in self.cumres_ng_paths]
    self.y_cumres_ng_paths = [os.path.join(r, 'y') for r in self.cumres_ng_paths]

    self.resup_ng_paths   = [os.path.join(dst_ng_path, 'vec_up/{}'.format(i))
                                                    for i in range(self.process_high_mip + 10)] #TODO
    self.x_resup_ng_paths = [os.path.join(r, 'x') for r in self.resup_ng_paths]
    self.y_resup_ng_paths = [os.path.join(r, 'y') for r in self.resup_ng_paths]

    self.cumresup_ng_paths   = [os.path.join(dst_ng_path, 'cumulative_vec_up/{}'.format(i))
                                                    for i in range(self.process_high_mip + 10)] #TODO
    self.x_cumresup_ng_paths = [os.path.join(r, 'x') for r in self.cumresup_ng_paths]
    self.y_cumresup_ng_paths = [os.path.join(r, 'y') for r in self.cumresup_ng_paths]

    self.field_ng_paths   = [os.path.join(dst_ng_path, 'field/{}'.format(i))
                                                    for i in range(self.process_high_mip + 10)] #TODO
    self.x_field_ng_paths = [os.path.join(r, 'x') for r in self.field_ng_paths]
    self.y_field_ng_paths = [os.path.join(r, 'y') for r in self.field_ng_paths]

    self.net = Process(model_path, mip_range[0], is_Xmas=is_Xmas, cuda=True, dim=high_mip_chunk[0]+crop*2, skip=skip, topskip=topskip, size=size, flip_average=flip_average, old_upsample=old_upsample)
    
    self.write_intermediaries = write_intermediaries
    self.upsample_residuals = upsample_residuals

    self.dst_chunk_sizes   = []
    self.dst_voxel_offsets = []
    self.vec_chunk_sizes   = []
    self.vec_voxel_offsets = []
    self.vec_total_sizes   = []
    self._create_info_files(max_displacement)
    self.pool = ThreadPool(threads)

    self.img_cache = {}

    self.img_cache_lock = Lock()

#if not chunk_size[0] :
    #  raise Exception("The chunk size has to be aligned with ng chunk size")

  def Gaussian_filter(self, kernel_size, sigma):
    x_cord = torch.arange(kernel_size)
    x_grid = x_cord.repeat(kernel_size).view(kernel_size, kernel_size)
    y_grid = x_grid.t()
    xy_grid = torch.stack([x_grid, y_grid], dim=-1)
    channels =1
    mean = (kernel_size - 1)/2.
    variance = sigma**2.
    gaussian_kernel = (1./(2.*math.pi*variance)) *np.exp(
        -torch.sum((xy_grid - mean)**2., dim=-1) /\
        (2*variance))
    gaussian_kernel = gaussian_kernel / torch.sum(gaussian_kernel)
    gaussian_kernel = gaussian_kernel.view(1, 1, kernel_size, kernel_size)
    gaussian_kernel = gaussian_kernel.repeat(channels, 1, 1, 1)
    gaussian_filter = nn.Conv2d(in_channels=channels, out_channels=channels,kernel_size=kernel_size, padding = (kernel_size -1)//2, bias=False)
    gaussian_filter.weight.data = gaussian_kernel.type(torch.float32)
    gaussian_filter.weight.requires_grad = False
    return gaussian_filter

  def set_chunk_size(self, chunk_size):
    self.high_mip_chunk = chunk_size

  def _create_info_files(self, max_offset):
    src_cv = cv(self.src_ng_path)
    src_info = src_cv.info
    m = len(src_info['scales'])
    each_factor = Vec(2,2,1)
    factor = Vec(2**m,2**m,1)
    for _ in range(m, self.process_low_mip + self.size):
      src_cv.add_scale(factor)
      factor *= each_factor
      chunksize = src_info['scales'][-2]['chunk_sizes'][0] // each_factor
      src_info['scales'][-1]['chunk_sizes'] = [ list(map(int, chunksize)) ]

    # print(src_info)
    dst_info = deepcopy(src_info)

    ##########################################################
    #### Create dst info file
    ##########################################################
    chunk_size = dst_info["scales"][0]["chunk_sizes"][0][0]
    dst_size_increase = max_offset
    if dst_size_increase % chunk_size != 0:
      dst_size_increase = dst_size_increase - (dst_size_increase % max_offset) + chunk_size
    scales = dst_info["scales"]
    for i in range(len(scales)):
      scales[i]["voxel_offset"][0] -= int(dst_size_increase / (2**i))
      scales[i]["voxel_offset"][1] -= int(dst_size_increase / (2**i))

      scales[i]["size"][0] += int(dst_size_increase / (2**i))
      scales[i]["size"][1] += int(dst_size_increase / (2**i))

      x_remainder = scales[i]["size"][0] % scales[i]["chunk_sizes"][0][0]
      y_remainder = scales[i]["size"][1] % scales[i]["chunk_sizes"][0][1]

      x_delta = 0
      y_delta = 0
      if x_remainder != 0:
        x_delta = scales[i]["chunk_sizes"][0][0] - x_remainder
      if y_remainder != 0:
        y_delta = scales[i]["chunk_sizes"][0][1] - y_remainder

      scales[i]["size"][0] += x_delta
      scales[i]["size"][1] += y_delta

      scales[i]["size"][0] += int(dst_size_increase / (2**i))
      scales[i]["size"][1] += int(dst_size_increase / (2**i))

      #make it slice-by-slice writable
      scales[i]["chunk_sizes"][0][2] = 1

      self.dst_chunk_sizes.append(scales[i]["chunk_sizes"][0][0:2])
      self.dst_voxel_offsets.append(scales[i]["voxel_offset"])

    cv(self.dst_ng_path, info=dst_info).commit_info()
    cv(self.tmp_ng_path, info=dst_info).commit_info()

    ##########################################################
    #### Create vec info file
    ##########################################################
    vec_info = deepcopy(src_info)
    vec_info["data_type"] = "float32"
    for i in range(len(vec_info["scales"])):
      vec_info["scales"][i]["chunk_sizes"][0][2] = 1

    enc_dict = {x: 6*(x-self.process_low_mip)+12 for x in 
                    range(self.process_low_mip, self.process_high_mip+1)} 

    scales = deepcopy(vec_info["scales"])
    # print('src_info scales: {0}'.format(len(scales)))
    if (self.run_pairs):
        field_sf_info = deepcopy(dst_info)
        field_sf_info["data_type"] = "float32"
        for i in range(len(field_sf_info["scales"])):
            field_sf_info["scales"][i]["chunk_sizes"][0][2] = 1
        field_sf_info['num_channels'] = 2
        cv(self.field_sf_ng_path, info=field_sf_info).commit_info() 
    print("scales len is ", len(scales))
    for i in range(len(scales) -1):
      self.vec_chunk_sizes.append(scales[i]["chunk_sizes"][0][0:2])
      self.vec_voxel_offsets.append(scales[i]["voxel_offset"])
      self.vec_total_sizes.append(scales[i]["size"])
      print("i is ", i)
      cv(self.x_field_ng_paths[i], info=vec_info).commit_info()
      cv(self.y_field_ng_paths[i], info=vec_info).commit_info()
      cv(self.x_res_ng_paths[i], info=vec_info).commit_info()
      cv(self.y_res_ng_paths[i], info=vec_info).commit_info()
      cv(self.x_cumres_ng_paths[i], info=vec_info).commit_info()
      cv(self.y_cumres_ng_paths[i], info=vec_info).commit_info()
      cv(self.x_resup_ng_paths[i], info=vec_info).commit_info()
      cv(self.y_resup_ng_paths[i], info=vec_info).commit_info()
      cv(self.x_cumresup_ng_paths[i], info=vec_info).commit_info()
      cv(self.y_cumresup_ng_paths[i], info=vec_info).commit_info()

      if i in enc_dict.keys():
        enc_info = deepcopy(vec_info)
        enc_info['num_channels'] = enc_dict[i]
        # enc_info['data_type'] = 'uint8'
        cv(self.enc_ng_paths[i], info=enc_info).commit_info()

  def check_all_params(self):
    return True

  def get_upchunked_bbox(self, bbox, ng_chunk_size, offset, mip):
    raw_x_range = bbox.x_range(mip=mip)
    raw_y_range = bbox.y_range(mip=mip)

    x_chunk = ng_chunk_size[0]
    y_chunk = ng_chunk_size[1]

    x_offset = offset[0]
    y_offset = offset[1]

    x_remainder = ((raw_x_range[0] - x_offset) % x_chunk)
    y_remainder = ((raw_y_range[0] - y_offset) % y_chunk)

    x_delta = 0
    y_delta = 0
    if x_remainder != 0:
      x_delta =  x_chunk - x_remainder
    if y_remainder != 0:
      y_delta =  y_chunk - y_remainder

    calign_x_range = [raw_x_range[0] + x_delta, raw_x_range[1]]
    calign_y_range = [raw_y_range[0] + y_delta, raw_y_range[1]]

    x_start = calign_x_range[0] - x_chunk
    y_start = calign_y_range[0] - y_chunk

    x_start_m0 = x_start * 2**mip
    y_start_m0 = y_start * 2**mip

    result = BoundingBox(x_start_m0, x_start_m0 + bbox.x_size(mip=0),
                         y_start_m0, y_start_m0 + bbox.y_size(mip=0),
                         mip=0, max_mip=self.process_high_mip)
    return result

  def get_field_sf_residual(self, z, bbox, mip):
    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)
    field_sf = cv(self.field_sf_ng_path, mip=mip, bounded=False, 
                  fill_missing=True, progress=False)[x_range[0]:x_range[1], 
                                                     y_range[0]:y_range[1], z]
    abs_res = np.expand_dims(np.squeeze(field_sf), axis=0)
    rel_res = self.abs_to_rel_residual(abs_res, bbox, mip)
    return rel_res
 
  def get_field_sf_residual_h5(self, z, bbox, mip):
      x_range = bbox.x_range(mip=mip)
      y_range = bbox.y_range(mip=mip)
      field_sf = cv(self.field_sf_ng_path, mip=mip, bounded=False, 
                    fill_missing=True, progress=False)[x_range[0]:x_range[1], 
                                                       y_range[0]:y_range[1], z]
      abs_res = np.expand_dims(np.squeeze(field_sf), axis=0)
      return abs_res

  def save_field_patch(self, field_sf, bbox, mip, z):
    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)
    new_field = np.squeeze(field_sf)[:, :, np.newaxis, :]
    cv(self.field_sf_ng_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
       progress=False)[x_range[0]:x_range[1], y_range[0]:y_range[1], z] = new_field



  def break_into_chunks(self, bbox, ng_chunk_size, offset, mip, render=False):
    chunks = []
    raw_x_range = bbox.x_range(mip=mip)
    raw_y_range = bbox.y_range(mip=mip)

    x_chunk = ng_chunk_size[0]
    y_chunk = ng_chunk_size[1]

    x_offset = offset[0]
    y_offset = offset[1]

    x_remainder = ((raw_x_range[0] - x_offset) % x_chunk)
    y_remainder = ((raw_y_range[0] - y_offset) % y_chunk)

    x_delta = 0
    y_delta = 0
    if x_remainder != 0:
      x_delta =  x_chunk - x_remainder
    if y_remainder != 0:
      y_delta =  y_chunk - y_remainder

    calign_x_range = [raw_x_range[0] - x_remainder, raw_x_range[1]]
    calign_y_range = [raw_y_range[0] - y_remainder, raw_y_range[1]]

    x_start = calign_x_range[0] - x_chunk
    y_start = calign_y_range[0] - y_chunk

    if (self.process_high_mip > mip):
        high_mip_scale = 2**(self.process_high_mip - mip)
    else:
        high_mip_scale = 1

    processing_chunk = (int(self.high_mip_chunk[0] * high_mip_scale),
                        int(self.high_mip_chunk[1] * high_mip_scale))
    if not render and (processing_chunk[0] > self.max_chunk[0]
                      or processing_chunk[1] > self.max_chunk[1]):
      processing_chunk = self.max_chunk
    elif render and (processing_chunk[0] > self.max_render_chunk[0]
                     or processing_chunk[1] > self.max_render_chunk[1]):
      processing_chunk = self.max_render_chunk

    for xs in range(calign_x_range[0], calign_x_range[1], processing_chunk[0]):
      for ys in range(calign_y_range[0], calign_y_range[1], processing_chunk[1]):
        chunks.append(BoundingBox(xs, xs + processing_chunk[0],
                                 ys, ys + processing_chunk[0],
                                 mip=mip, max_mip=self.high_mip))

    return chunks

  def compute_residual_patch(self, source_z, target_z, out_patch_bbox, mip):
    #print ("Computing residual for region {}.".format(out_patch_bbox.__str__(mip=0)), flush=True)
    precrop_patch_bbox = deepcopy(out_patch_bbox)
    precrop_patch_bbox.uncrop(self.crop_amount, mip=mip)

    if mip == self.process_high_mip:
      src_patch = self.get_image_data(self.src_ng_path, source_z, precrop_patch_bbox, mip)
    else:
      src_patch = self.get_image_data(self.tmp_ng_path, source_z, precrop_patch_bbox, mip)

    if (self.run_pairs):
         # only align consecutive pairs of source slices TODO: write function to compse resulting vector fields
        tgt_patch = self.get_image_data(self.src_ng_path, target_z, precrop_patch_bbox, mip, should_backtrack=True)
    else:
        # align to the newly aligned previous slice
        tgt_patch = self.get_image_data(self.dst_ng_path, target_z, precrop_patch_bbox, mip, should_backtrack=True)
    field, residuals, encodings, cum_residuals = self.net.process(src_patch, tgt_patch, mip, crop=self.crop_amount, old_vectors=self.old_vectors)
    rel_residual = precrop_patch_bbox.spoof_x_y_residual(1024, 0, mip=mip,
                            crop_amount=self.crop_amount)

    # save the final vector field for warping
    #offset = 1949 
    #field = self.get_field_from_h5(out_patch_bbox, source_z - offset)
    self.save_vector_patch(field, self.x_field_ng_paths[mip], self.y_field_ng_paths[mip], source_z, out_patch_bbox, mip)

    if self.write_intermediaries:
  
      mip_range = range(self.process_low_mip+self.size-1, self.process_low_mip-1, -1)
      for res_mip, res, cumres in zip(mip_range, residuals[1:], cum_residuals[1:]):
          crop = self.crop_amount // 2**(res_mip - self.process_low_mip)   
          self.save_residual_patch(res, crop, self.x_res_ng_paths[res_mip], 
                                   self.y_res_ng_paths[res_mip], source_z, 
                                   out_patch_bbox, res_mip)
          self.save_residual_patch(cumres, crop, self.x_cumres_ng_paths[res_mip], 
                                   self.y_cumres_ng_paths[res_mip], source_z, 
                                   out_patch_bbox, res_mip)
          if self.upsample_residuals:
            crop = self.crop_amount   
            res = self.scale_residuals(res, res_mip, self.process_low_mip)
            self.save_residual_patch(res, crop, self.x_resup_ng_paths[res_mip], 
                                     self.y_resup_ng_paths[res_mip], source_z, 
                                     out_patch_bbox, self.process_low_mip)
            cumres = self.scale_residuals(cumres, res_mip, self.process_low_mip)
            self.save_residual_patch(cumres, crop, self.x_cumresup_ng_paths[res_mip], 
                                     self.y_cumresup_ng_paths[res_mip], source_z, 
                                     out_patch_bbox, self.process_low_mip)


 
      # print('encoding size: {0}'.format(len(encodings)))
      for k, enc in enumerate(encodings):
          mip = self.process_low_mip + k
          # print('encoding shape @ idx={0}, mip={1}: {2}'.format(k, mip, enc.shape))
          crop = self.crop_amount // 2**k
          enc = enc[:,:,crop:-crop, crop:-crop].permute(2,3,0,1)
          enc = enc.data.cpu().numpy()
          
          def write_encodings(j_slice, z):
            x_range = out_patch_bbox.x_range(mip=mip)
            y_range = out_patch_bbox.y_range(mip=mip)
            patch = enc[:, :, :, j_slice]
            # uint_patch = (np.multiply(patch, 255)).astype(np.uint8)
            cv(self.enc_ng_paths[mip], mip=mip, bounded=False, fill_missing=True, autocrop=True,
                                    progress=False)[x_range[0]:x_range[1],
                                                    y_range[0]:y_range[1], z, j_slice] = patch # uint_patch
  
          # src_image encodings
          write_encodings(slice(0, enc.shape[-1] // 2), source_z)
          # dst_image_encodings
          write_encodings(slice(enc.shape[-1] // 2, enc.shape[-1]), target_z)
        
    
  def abs_to_rel_residual(self, abs_residual, patch, mip):
    x_fraction = patch.x_size(mip=0) * 0.5
    y_fraction = patch.y_size(mip=0) * 0.5

    rel_residual = deepcopy(abs_residual)
    rel_residual[0, :, :, 0] /= x_fraction
    rel_residual[0, :, :, 1] /= y_fraction
    return rel_residual

  def calc_image_mean_field(self, image, field, cid):
      for i in range(0,image.shape[0]):
          for j in range(0,image.shape[1]):
              if(image[i,j]!=0):
                  self.image_pixels_sum[cid] +=1
                  self.field_sf_sum[cid] += field[i,j]

  def get_bbox_id(self, in_bbox, mip):
    raw_x_range = self.total_bbox.x_range(mip=mip)
    raw_y_range = self.total_bbox.y_range(mip=mip)

    x_chunk = self.dst_chunk_sizes[mip][0]
    y_chunk = self.dst_chunk_sizes[mip][1]

    x_offset = self.dst_voxel_offsets[mip][0]
    y_offset = self.dst_voxel_offsets[mip][1]

    x_remainder = ((raw_x_range[0] - x_offset) % x_chunk)
    y_remainder = ((raw_y_range[0] - y_offset) % y_chunk)
     
    calign_x_range = [raw_x_range[0] - x_remainder, raw_x_range[1]]
    calign_y_range = [raw_y_range[0] - y_remainder, raw_y_range[1]]

    calign_x_len = raw_x_range[1] - raw_x_range[0] + x_remainder
    #calign_y_len = raw_y_range[1] - raw_y_range[0] + y_remainder

    in_x_range = in_bbox.x_range(mip=mip)
    in_y_range = in_bbox.y_range(mip=mip)
    in_x_len = in_x_range[1] - in_x_range[0]
    in_y_len = in_y_range[1] - in_y_range[0]
    line_bbox_num = (calign_x_len + in_x_len -1)// in_x_len
    cid = ((in_y_range[0] - calign_y_range[0]) // in_y_len) * line_bbox_num + (in_x_range[0] - calign_x_range[0]) // in_x_len
    return cid

    

  ## Patch manipulation
  def warp_patch(self, ng_path, z, bbox, res_mip_range, mip, start_z=-1):
    influence_bbox = deepcopy(bbox)
    influence_bbox.uncrop(self.max_displacement, mip=0)
    start = time()
    offset = 1949
    #print(" index is ", z - offset) 
    agg_flow = self.get_field_from_h5(influence_bbox, z - offset)
    #agg_flow = self.get_aggregate_rel_flow(z, influence_bbox, res_mip_range, mip)
    image = torch.from_numpy(self.get_image_data(ng_path, z, influence_bbox, mip))
    image = image.unsqueeze(0)
    mip_disp = int(self.max_displacement / 2**mip)
    agg_flow =  2 * agg_flow / (image.shape[-1])
    #agg_flow = agg_flow.permute(0,2,1,3) #27
    #agg_flow = agg_flow.flip(3)
    #no need to warp if flow is identity since warp introduces noise
    #print("agg_flow", agg_flow.shape, "image ", image.shape, agg_flow)
    if torch.min(agg_flow) != 0 or torch.max(agg_flow) != 0:
      image = gridsample_residual(image, agg_flow, padding_mode='zeros')
    else:
      print ("not warping")
    if (self.run_pairs):
      #cid = self.get_bbox_id(bbox, mip) 
      #print ("cid is ", cid)
      if z != start_z:
        #field_sf = torch.from_numpy(self.get_field_sf_residual(z-1, influence_bbox, mip))
        field_sf = torch.from_numpy(self.get_field_sf_residual_h5(z+1, influence_bbox, mip))
        regular_part_x = torch.from_numpy(scipy.ndimage.filters.gaussian_filter((field_sf[...,0]), 128)).unsqueeze(-1)
        regular_part_y = torch.from_numpy(scipy.ndimage.filters.gaussian_filter((field_sf[...,1]), 128)).unsqueeze(-1)
        #regular_part = self.gauss_filter(field_sf.permute(3,0,1,2))
        #regular_part = torch.from_numpy(self.reg_field) 
        #field_sf = decay_factor * field_sf + (1 - decay_factor) * regular_part.permute(1,2,3,0) 
        #field_sf = regular_part.permute(1,2,3,0) 
        field_sf = torch.cat([regular_part_x,regular_part_y],-1)
        image = gridsample_residual(image, field_sf, padding_mode='zeros')
        agg_flow = agg_flow.permute(0,3,1,2)
        field_sf = field_sf + gridsample_residual(
            agg_flow, field_sf, padding_mode='border').permute(0,2,3,1)
      else:
        field_sf = agg_flow
      #field_sf = field_sf * (field_sf.shape[-2] / 2) * (2**mip)
      field_sf = field_sf.numpy()[:, mip_disp:-mip_disp, mip_disp:-mip_disp, :]
      self.save_field_patch(field_sf, bbox, mip, z)

    return image.numpy()[0,:,mip_disp:-mip_disp,mip_disp:-mip_disp]

  def downsample_patch(self, ng_path, z, bbox, mip):
    in_data = self.get_image_data(ng_path, z, bbox, mip - 1)
    result = np_downsample(in_data, 2)
    return result

  ## Data saving
  def save_image_patch(self, ng_path, float_patch, z, bbox, mip):
    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)
    patch = float_patch[0, :, :, np.newaxis]
    uint_patch = (patch).astype(np.uint8)
    #uint_patch = (np.multiply(patch, 255)).astype(np.uint8)
    cv(ng_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
                                  progress=False)[x_range[0]:x_range[1],
                                                  y_range[0]:y_range[1], z] = uint_patch

  def scale_residuals(self, res, src_mip, dst_mip):
    print('Upsampling residuals from MIP {0} to {1}'.format(src_mip, dst_mip))
    up = nn.Upsample(scale_factor=2, mode='bilinear')
    for m in range(src_mip, dst_mip, -1):
      res = up(res.permute(0,3,1,2)).permute(0,2,3,1)
    return res

  def save_residual_patch(self, res, crop, x_path, y_path, z, bbox, mip):
    print ("Saving residual patch {} at MIP {}".format(bbox.__str__(mip=0), mip))
    v = res * (res.shape[-2] / 2) * (2**mip)
    v = v[:,crop:-crop, crop:-crop,:]
    v = v.data.cpu().numpy() 
    self.save_vector_patch(v, x_path, y_path, z, bbox, mip)

  def save_vector_patch(self, flow, x_path, y_path, z, bbox, mip):
    x_res = flow[0, :, :, 0, np.newaxis]
    y_res = flow[0, :, :, 1, np.newaxis]

    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)
    print("x_range", x_range, "y_range", y_range)
    #cv(x_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
    #                   non_aligned_writes=False, progress=False)[x_range[0]:x_range[1],
    #                                               y_range[0]:y_range[1], z] = x_res
    #cv(y_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
    #                   non_aligned_writes=False, progress=False)[x_range[0]:x_range[1],
    #                                               y_range[0]:y_range[1], z] = y_res
    cv(x_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
                       non_aligned_writes=True, progress=False)[x_range[0]:x_range[1],
                                                   y_range[0]:y_range[1], z] = x_res
    cv(y_path, mip=mip, bounded=False, fill_missing=True, autocrop=True,
                       non_aligned_writes=True, progress=False)[x_range[0]:x_range[1],
                                                   y_range[0]:y_range[1], z] = y_res


  ## Data loading
  def preprocess_data(self, data):
    sd = np.squeeze(data)
    ed = np.expand_dims(sd, 0)
    #nd = np.divide(ed, float(255.0), dtype=np.float32)
    nd = ed.astype(np.float32)
    return nd

  def dilate_mask(self, mask, radius=5):
    return skmaximum(np.squeeze(mask).astype(np.uint8), skdisk(radius)).reshape(mask.shape).astype(np.bool)
    
  def missing_data_mask(self, img, bbox, mip):
    (img_xs, img_xe), (img_ys, img_ye) = bbox.x_range(mip=mip), bbox.y_range(mip=mip)
    (total_xs, total_xe), (total_ys, total_ye) = self.total_bbox.x_range(mip=mip), self.total_bbox.y_range(mip=mip)
    xs_inset = max(0, total_xs - img_xs)
    xe_inset = max(0, img_xe - total_xe)
    ys_inset = max(0, total_ys - img_ys)
    ye_inset = max(0, img_ye - total_ye)
    mask = np.logical_or(img == 0, img >= 253)
    
    fov_mask = np.ones(mask.shape).astype(np.bool)
    if xs_inset > 0:
      fov_mask[:xs_inset] = False
    if xe_inset > 0:
      fov_mask[-xe_inset:] = False
    if ys_inset > 0:
      fov_mask[:,:ys_inset] = False
    if ye_inset > 0:
      fov_mask[:,-ye_inset:] = False

    return np.logical_and(fov_mask, mask)
    
  def supplement_target_with_backup(self, target, still_missing_mask, backup, bbox, mip):
    backup_missing_mask = self.missing_data_mask(backup, bbox, mip)
    fill_in = backup_missing_mask < still_missing_mask
    target[fill_in] = backup[fill_in]

  def check_image_cache(self, path, bbox, mip):
    with self.img_cache_lock:
      output = -1 * np.ones((1,1,bbox.x_size(mip), bbox.y_size(mip)))
      for key in self.img_cache:
        other_path, other_bbox, other_mip = key[0], key[1], key[2]
        if other_mip == mip and other_path == path:
          if bbox.intersects(other_bbox):
            xs, ys, xsz, ysz = other_bbox.insets(bbox, mip)
            output[:,:,xs:xs+xsz,ys:ys+ysz] = self.img_cache[key]
    if np.min(output > -1):
      print('hit')
      return output
    else:
      return None

  def add_to_image_cache(self, path, bbox, mip, data):
    with self.img_cache_lock:
      self.img_cache[(path, bbox, mip)] = data

  def get_image_data(self, path, z, bbox, mip, should_backtrack=False):
    #data = self.check_image_cache(path, bbox, mip)
    #if data is not None:
    #  return data
    data = None
    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)
    while data is None:
      try:
        data_ = cv(path, mip=mip, progress=False,
                   bounded=False, fill_missing=True)[x_range[0]:x_range[1], y_range[0]:y_range[1], z]
        data = data_
      except AttributeError as e:
        pass
    
    if self.num_targets > 1 and should_backtrack:
      for backtrack in range(1, self.num_targets):
        if z-backtrack < self.zs:
          break
        still_missing_mask = self.missing_data_mask(data, bbox, mip)
        if not np.any(still_missing_mask):
          break # we've got a full slice
        backup = None
        while backup is None:
          try:
            backup_ = cv(path, mip=mip, progress=False,
                         bounded=False, fill_missing=True)[x_range[0]:x_range[1], y_range[0]:y_range[1], z-backtrack]
            backup = backup_
          except AttributeError as e:
            pass
          
        self.supplement_target_with_backup(data, still_missing_mask, backup, bbox, mip)
        
    data = self.preprocess_data(data)
    #self.add_to_image_cache(path, bbox, mip, data)

    return data

  def get_vector_data(self, path, z, bbox, mip):
    x_range = bbox.x_range(mip=mip)
    y_range = bbox.y_range(mip=mip)

    data = None
    while data is None:
      try:
        data_ = cv(path, mip=mip, progress=False,
                   bounded=False, fill_missing=True)[x_range[0]:x_range[1], y_range[0]:y_range[1], z]
        data = data_
      except AttributeError as e:
        pass
    return data

  def get_abs_residual(self, z, bbox, mip):
    x = self.get_vector_data(self.x_field_ng_paths[mip], z, bbox, mip)[..., 0, 0]
    y = self.get_vector_data(self.y_field_ng_paths[mip], z, bbox, mip)[..., 0, 0]
    result = np.stack((x, y), axis=2)
    return np.expand_dims(result, axis=0)

  def get_rel_residual(self, z, bbox, mip):
    x = self.get_vector_data(self.x_field_ng_paths[mip], z, bbox, mip)[..., 0, 0]
    y = self.get_vector_data(self.y_field_ng_paths[mip], z, bbox, mip)[..., 0, 0]
    abs_res = np.stack((x, y), axis=2)
    abs_res = np.expand_dims(abs_res, axis=0)
    rel_res = self.abs_to_rel_residual(abs_res, bbox, mip)
    return rel_res


  def get_aggregate_rel_flow(self, z, bbox, res_mip_range, mip):
    result = torch.zeros((1, bbox.x_size(mip), bbox.y_size(mip), 2), dtype=torch.float)
    start_mip = max(res_mip_range[0], self.process_low_mip)
    end_mip   = min(res_mip_range[1], self.process_high_mip)

    for res_mip in range(start_mip, end_mip + 1):
      rel_res = torch.from_numpy(self.get_rel_residual(z, bbox, res_mip))
      up_rel_res = upsample(res_mip - mip)(rel_res.permute(0,3,1,2)).permute(0,2,3,1)
      result += up_rel_res

    return result

  ## High level services
  def copy_section(self, source, dest, z, bbox, mip):
    print ("moving section {} mip {} to dest".format(z, mip), end='', flush=True)
    start = time()
    chunks = self.break_into_chunks(bbox, self.dst_chunk_sizes[mip],
                                    self.dst_voxel_offsets[mip], mip=mip, render=True)
    #for patch_bbox in chunks:
    def chunkwise(patch_bbox):
      raw_patch = self.get_image_data(source, z, patch_bbox, mip)
      self.save_image_patch(dest, raw_patch, z, patch_bbox, mip)

    self.pool.map(chunkwise, chunks)

    end = time()
    print (": {} sec".format(end - start))

  def prepare_source(self, z, bbox, mip):
    print ("Prerendering mip {}".format(mip),
           end='', flush=True)
    start = time()

    chunks = self.break_into_chunks(bbox, self.dst_chunk_sizes[mip],
                                    self.dst_voxel_offsets[mip], mip=mip, render=True)

    def chunkwise(patch_bbox):
      warped_patch = self.warp_patch(self.src_ng_path, z, patch_bbox,
                                     (mip + 1, self.process_high_mip), mip)
      self.save_image_patch(self.tmp_ng_path, warped_patch, z, patch_bbox, mip)
    self.pool.map(chunkwise, chunks)
    end = time()
    print (": {} sec".format(end - start))

  def render(self, z, bbox, mip, start_z):
    print ("Rendering slice {} mip {}".format(z, mip),
              end='', flush=True)
    start = time()
    chunks = self.break_into_chunks(bbox, self.dst_chunk_sizes[mip],
                                    self.dst_voxel_offsets[mip], mip=mip, render=True)
    def chunkwise(patch_bbox):
      warped_patch = self.warp_patch(self.src_ng_path, z, patch_bbox,
                                    (mip, self.process_high_mip), mip, start_z)
      self.save_image_patch(self.dst_ng_path, warped_patch, z, patch_bbox, mip)
    self.pool.map(chunkwise, chunks)
    end = time()
    print (": {} sec".format(end - start))

  def render_section_all_mips(self, z, bbox, start_z):
    self.render(z, bbox, self.render_low_mip, start_z)
    self.downsample(z, bbox, self.render_low_mip, self.render_high_mip)

  def to_cv(self, z, bbox): 
    influence_bbox = deepcopy(bbox)
    influence_bbox.uncrop(self.max_displacement, mip=0)
    start = time()
    offset = 1949
    mip_disp = int(self.max_displacement / 2**8) 
    agg_flow = self.get_field_from_h5(influence_bbox, z - offset)
    #print("agg_flow shape", agg_flow.shape)
    self.save_vector_patch(agg_flow.numpy()[:, mip_disp:-mip_disp, mip_disp:-mip_disp, :], self.x_field_ng_paths[8], self.y_field_ng_paths[8], z, bbox, 8)

  def upload_to_cv(self, z, bbox):
    chunks = self.break_into_chunks(bbox, self.dst_chunk_sizes[8],
                                   self.dst_voxel_offsets[8], mip=8, render=True)
    for i in chunks:
      x_range = i.x_range(mip=8)
      y_range = i.y_range(mip=8) 
      print("break chunks: x_range", x_range, "y_range", y_range)

    def chunkwise(patch_bbox):
      self.to_cv(z, patch_bbox)
    self.pool.map(chunkwise, chunks)


  def downsample(self, z, bbox, source_mip, target_mip):
    print ("Downsampling {} from mip {} to mip {}".format(bbox.__str__(mip=0), source_mip, target_mip))
    for m in range(source_mip+1, target_mip + 1):
      chunks = self.break_into_chunks(bbox, self.dst_chunk_sizes[m],
                                      self.dst_voxel_offsets[m], mip=m, render=True)

      def chunkwise(patch_bbox):
        print ("Downsampling {} to mip {}".format(patch_bbox.__str__(mip=0), m))
        downsampled_patch = self.downsample_patch(self.dst_ng_path, z, patch_bbox, m)
        self.save_image_patch(self.dst_ng_path, downsampled_patch, z, patch_bbox, m)
      self.pool.map(chunkwise, chunks)

  def compute_section_pair_residuals(self, source_z, target_z, bbox):
    for m in range(self.process_high_mip,  self.process_low_mip - 1, -1):
      start = time()
      chunks = self.break_into_chunks(bbox, self.vec_chunk_sizes[m],
                                      self.vec_voxel_offsets[m], mip=m)
      print ("Aligning slice {} to slice {} at mip {} ({} chunks)".
             format(source_z, target_z, m, len(chunks)), flush=True)

      #for patch_bbox in chunks:
      def chunkwise(patch_bbox):
      #FIXME Torch runs out of memory
      #FIXME batchify download and upload
        self.compute_residual_patch(source_z, target_z, patch_bbox, mip=m)
      self.pool.map(chunkwise, chunks)
      end = time()
      print (": {} sec".format(end - start))

      if m > self.process_low_mip:
          self.prepare_source(source_z, bbox, m - 1)
    
  def read_h5(self):
      file_name = "/usr/people/zhenj/mnt/zhenj/vecs_for_zhen.h5"
      f = h5py.File(file_name, 'r')
      return f
  
  def get_field_from_h5(self, bbox, z):
      mip = 8
      x_range = bbox.x_range(mip=mip) # get data accorrding to mip8 scale
      y_range = bbox.y_range(mip=mip)
      #print("z is ", z, "xrange", x_range, "y_range", y_range) 
      #field = torch.from_numpy(self.vector_field_file["/main"].
      #                         value[z,x_range[0]:x_range[1],y_range[0]:y_range[1],:])
      offset = 351
      end = 1152
      end_offset = offset + end
      x_range_mip8 = list(x_range)
      y_range_mip8 = list(y_range)
      if x_range[0] < offset:
          x_range_mip8[0] = 0
          x_range_mip8[1] = x_range[1] - offset
          x_start = offset - x_range[0]
          x_end = x_range[1] - x_range[0] 
      elif x_range[1] > end_offset:
          x_range_mip8[1] = end
          x_range_mip8[0] = x_range[0] - offset
          x_start = 0
          x_end = end_offset - x_range[0] 
      else:
          x_range_mip8[0] = x_range[0] - offset
          x_range_mip8[1] = x_range[1] - offset
          x_start = 0
          x_end = x_range[1] - x_range[0]
      if y_range[0] < offset:
          y_range_mip8[0] = 0
          y_range_mip8[1] = y_range[1] - offset
          y_start = offset - y_range[0]
          y_end = y_range[1] - y_range[0] 
      elif y_range[1] > end_offset:
          y_range_mip8[1] = end
          y_range_mip8[0] = y_range[0] - offset
          y_start = 0
          y_end = end_offset - y_range[0]
      else:
          y_range_mip8[0] = y_range[0] - offset
          y_range_mip8[1] = y_range[1] - offset
          y_start = 0
          y_end = y_range[1] - y_range[0]

      field = torch.zeros(x_range[1] - x_range[0], y_range[1] - y_range[0], 2, dtype=torch.float)
      field_h5 = torch.from_numpy(self.vector_field_file["/main"].
                               value[z,x_range_mip8[0]:x_range_mip8[1],y_range_mip8[0]:y_range_mip8[1],:]).cpu()
      print("field shape is", field.shape, "field_h5 shape is", field_h5.shape, "xrange", x_range, "y_range", y_range, "x_rangeMip8", x_range_mip8, "y_rangeMip8", y_range_mip8, "x slice", x_start, x_end, "y slice", y_start, y_end, bbox.x_range(mip=0))
      field[x_start:x_end, y_start:y_end, :] = field_h5 
      #print("xrange", x_range, "y_range", y_range, "x_rangeMip8", x_range_mip8, "y_rangeMip8", y_range_mip8, "x slice", x_start, x_end, "y slice", y_start, y_end)  
      field_new = field #.flip(2)  
      field_for_ups = field_new.permute(2,0,1).unsqueeze(0)
      mip_field = upsample(mip - self.render_low_mip)(field_for_ups)
      mip_field = mip_field.permute(0,2,3,1)
      mip_field = mip_field * (2**(mip - self.render_low_mip))
      #print("mip is ", mip ,"low_mip is", self.render_low_mip)
      #print("mip_field shape is", mip_field.shape,"xrange", x_range, "y_range", y_range, "field,shape", field.shape) 
      #print("mip_field shape is", mip_field.shape,"xrange", x_range, "y_range", y_range, "field,shape", field.shape, "x_rangeMip8", x_range_mip8, "y_rangeMip8", y_range_mip8) 
      #mip_field[...,1] = mip_field[...,1] * (2**(mip - self.render_low_mip))
      #mip_field[...,0] = mip_field[...,0] / (2**(self.render_low_mip))
      #mip_field[...,1] = mip_field[...,1] / (2**(self.render_low_mip))
      #mip_field = self.abs_to_rel_residual(mip_field, bbox, self.render_low_mip) 
      return mip_field 
  
  def uploat_field_to_cv(self, start_section, end_section, bbox):
    self.vector_field_file = self.read_h5()
    self.total_bbox = bbox
    start_z = start_section 
    start = time()
    start_z = start_section - 1 
    self.zs = start_section
    x_range = bbox.x_range(mip=8)
    y_range = bbox.y_range(mip=8)
    for z in range(start_section, end_section, -1):
      self.upload_to_cv(z - 1, bbox)
    end = time()
    print ("Total time for aligning {} slices: {}".format(end_section - start_section, end - start))  
  
  ## Whole stack operations
  def align_ng_stack(self, start_section, end_section, bbox, move_anchor=True):
    if not self.check_all_params():
      raise Exception("Not all parameters are set")
    #if not bbox.is_chunk_aligned(self.dst_ng_path):
    #  raise Exception("Have to align a chunkaligned size")
    self.vector_field_file = self.read_h5()
    self.total_bbox = bbox
    start_z = start_section 
    start = time()
    if move_anchor:
      for m in range(self.render_low_mip, self.high_mip+1):
        self.copy_section(self.src_ng_path, self.dst_ng_path, start_section, bbox, mip=m)
      start_z = start_section - 1 
    start_z = start_section - 1 
    self.zs = start_section
    for z in range(start_section, end_section, -1):
      self.img_cache = {}
      #self.compute_section_pair_residuals(z - 1, z, bbox)
      self.render_section_all_mips(z - 1, bbox, start_z)
    end = time()
    print ("Total time for aligning {} slices: {}".format(end_section - start_section, end - start))
 
