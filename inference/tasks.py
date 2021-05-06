import boto3
from time import time
import json
import tenacity
import numpy as np
from functools import partial
from mipless_cloudvolume import deserialize_miplessCV as DCV
from cloudvolume import Storage, CloudVolume
from cloudvolume.lib import scatter 
from boundingbox import BoundingBox, deserialize_bbox

from taskqueue import RegisteredTask, TaskQueue, LocalTaskQueue
from concurrent.futures import ProcessPoolExecutor
# from taskqueue.taskqueue import _scatter as scatter

def remote_upload(queue_name, ptasks):
  with TaskQueue(queue_name=queue_name) as tq:
    for task in ptasks:
      tq.insert(task)

def run(aligner, tasks): 
  if aligner.distributed:
    tasks = scatter(tasks, aligner.threads)
    fn = partial(remote_upload, aligner.queue_name)
    with ProcessPoolExecutor(max_workers=aligner.threads) as executor:
      executor.map(fn, tasks)
  else:
    with LocalTaskQueue(queue_name=aligner.queue_name, parallel=1) as tq:
      for task in tasks:
        tq.insert(task, args=[ aligner ])

class PredictImageTask(RegisteredTask):
  def __init__(self, model_path, src_cv, dst_cv, z, mip, bbox, overlap, prefix):
    super().__init__(model_path, src_cv, dst_cv, z, mip, bbox, overlap, prefix)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv)
    dst_cv = DCV(self.dst_cv)
    z = self.z
    mip = self.mip
    overlap = self.overlap
    overlap_bbox = np.array(overlap)*(2**mip)
    patch_bbox_in = deserialize_bbox(self.bbox)
    patch_bbox_in.extend(overlap_bbox)
    patch_range = patch_bbox_in.range(mip)
    patch_bbox_out = deserialize_bbox(self.bbox)
    patch_size = patch_bbox_out.size(mip)
    prefix = self.prefix

    print("\nPredict Image\n"
          "src {}\n"
          "dst {}\n"
          "at z={}\n"
          "MIP{}\n".format(src_cv, dst_cv, z, mip), flush=True)
    start = time()

    chunk_size = (320,320)
    image = aligner.predict_image_chunk(self.model_path, src_cv, z, mip, patch_bbox_in, chunk_size, overlap)
    image = image.cpu().numpy()
    min_bound = src_cv[mip].bounds.minpt
    image = image[(slice(0,1),slice(0,1),)+tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    aligner.save_image(image, dst_cv, z, patch_bbox_out, mip)

    # with Storage(dst_cv.path) as stor:
    #     path = 'predict_image_done/{}/{}'.format(prefix, patch_bbox_out.stringify(z))
    #     stor.put_file(path, '')
    #     print('Marked finished at {}'.format(path))
    end = time()
    diff = end - start
    print(':{:.3f} s'.format(diff))

class PredictMultiImageTask(RegisteredTask):
  def __init__(self, model_path, src_cv, dst1_cv, dst2_cv, z, mip, bbox, overlap, prefix):
    super().__init__(model_path, src_cv, dst1_cv, dst2_cv, z, mip, bbox, overlap, prefix)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv)
    dst1_cv = DCV(self.dst1_cv)
    dst2_cv = DCV(self.dst2_cv)
    z = self.z
    mip = self.mip
    overlap = self.overlap
    overlap_bbox = np.array(overlap)*(2**mip)
    patch_bbox_in = deserialize_bbox(self.bbox)
    patch_bbox_in.extend(overlap_bbox)
    patch_range = patch_bbox_in.range(mip)
    patch_bbox_out = deserialize_bbox(self.bbox)
    patch_size = patch_bbox_out.size(mip)
    prefix = self.prefix

    print("\nPredict Image\n"
          "src {}\n"
          "dst1 {}\n"
          "dst2 {}\n"
          "at z={}\n"
          "MIP{}\n".format(src_cv, dst1_cv, dst2_cv, z, mip), flush=True)
    start = time()

    chunk_size = (320,320)
    image = aligner.predict_image_chunk(self.model_path, src_cv, z, mip, patch_bbox_in, chunk_size, overlap, n_pred=2)
    image = image.cpu().numpy()
    min_bound = src_cv[mip].bounds.minpt
    image1 = image[(slice(0,1),slice(0,1),)+tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    image2 = image[(slice(0,1),slice(1,2),)+tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
#    image1 = image1*(image1>=image2)
#    image2 = image2*(image2>=image1)
    aligner.save_image(image1, dst1_cv, z, patch_bbox_out, mip)
    aligner.save_image(image2, dst2_cv, z, patch_bbox_out, mip)

    # with Storage(dst1_cv.path) as stor:
    #     path = 'predict_image_done/{}/{}'.format(prefix, patch_bbox_out.stringify(z))
    #     stor.put_file(path, '')
    #     print('Marked finished at {}'.format(path))
    # with Storage(dst2_cv.path) as stor:
    #     path = 'predict_image_done/{}/{}'.format(prefix, patch_bbox_out.stringify(z))
    #     stor.put_file(path, '')
    #     print('Marked finished at {}'.format(path))
    end = time()
    diff = end - start
    print(':{:.3f} s'.format(diff))

class FilterMaskSizeTask(RegisteredTask):
  def __init__(self, src_cv, dst_cv, patch_bbox, overlap, mip, z, thr_filter):
    super(). __init__(src_cv, dst_cv, patch_bbox, overlap, mip, z, thr_filter)

  def execute(self, aligner):
    cv = DCV(self.src_cv)
    dst_cv = DCV(self.dst_cv)
    mip = self.mip
    overlap = self.overlap
    # bbox = deserialize_bbox(self.patch_bbox)
    z = self.z
    overlap_bbox = np.array(overlap)*(2**mip)
    patch_bbox_in = deserialize_bbox(self.patch_bbox)
    patch_bbox_in.extend(overlap_bbox)
    patch_range = patch_bbox_in.range(mip)
    patch_bbox_out = deserialize_bbox(self.patch_bbox)
    patch_size = patch_bbox_out.size(mip)
    
    temp_image = aligner.simple_size_filter_chunk(cv, patch_bbox_in, mip, self.z, self.thr_filter)
    image = temp_image[np.newaxis,np.newaxis,...]
    min_bound = cv[mip].bounds.minpt
    image = image[(slice(0,1),slice(0,1),)+
                  tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),
                    overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    aligner.save_image(image, dst_cv, self.z, patch_bbox_out, mip, to_uint8=True)
    # image = aligner.get_image(cv, z, bbox, mip, to_tensor=False, to_float=False)



class ThresholdAndMaskTask(RegisteredTask):
  def __init__(self, src_cv_path, dst_cv_path, patch_bbox, mip, z, threshold):
    super(). __init__(src_cv_path, dst_cv_path, patch_bbox, mip, z, threshold)

  def execute(self, aligner):
    cv = DCV(self.src_cv_path)
    dst_cv = DCV(self.dst_cv_path)
    mip = self.mip
    z = self.z
    bbox = deserialize_bbox(self.patch_bbox)
    image = aligner.get_image(cv, z, bbox, mip, to_tensor=False, to_float=False)
    # x_range = bbox.x_range(mip)
    # y_range = bbox.y_range(mip)
    # image = cv[x_range,y_range,z]
    mask = image < self.threshold
    # import ipdb
    # ipdb.set_trace()
    # import ipdb
    # ipdb.set_trace()
    image[mask] = 0
    image[~mask] = 1
    # temp_image = aligner.calculate_fold_lengths_chunk(cv, patch_bbox_in, mip, self.z, self.thr_binarize, self.w_connect, self.thr_filter, self.return_skeleys)
    # image = temp_image[np.newaxis,np.newaxis,...]
    # min_bound = cv[mip].bounds.minpt
    # image = image[(slice(0,1),slice(0,1),)+
                  # tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),
                    # overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    aligner.save_image(image, dst_cv, z, bbox, mip, to_uint8=True, render_mip=0)

class FoldLengthCalcTask(RegisteredTask):
  def __init__(self, cv, dst_cv, patch_bbox, overlap, mip, z, thr_binarize, w_connect, thr_filter, return_skeleys=False):
    super(). __init__(cv, dst_cv, patch_bbox, overlap, mip, z, thr_binarize, w_connect, thr_filter, return_skeleys)

  def execute(self, aligner):
    cv = DCV(self.cv)
    dst_cv = DCV(self.dst_cv)
    overlap = self.overlap
    mip = self.mip
    
    overlap_bbox = np.array(overlap)*(2**mip)
    patch_bbox_in = deserialize_bbox(self.patch_bbox)
    patch_bbox_in.extend(overlap_bbox)
    patch_range = patch_bbox_in.range(mip)
    patch_bbox_out = deserialize_bbox(self.patch_bbox)
    patch_size = patch_bbox_out.size(mip)
    
    temp_image = aligner.calculate_fold_lengths_chunk(cv, patch_bbox_in, mip, self.z, self.thr_binarize, self.w_connect, self.thr_filter, self.return_skeleys)
    image = temp_image[np.newaxis,np.newaxis,...]
    min_bound = cv[mip].bounds.minpt
    image = image[(slice(0,1),slice(0,1),)+
                  tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),
                    overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    aligner.save_image(image, dst_cv, self.z, patch_bbox_out, mip, to_uint8=False, render_mip=0)

class FoldDetecPostTask(RegisteredTask):
  def __init__(self, cv, dst_cv, patch_bbox, overlap, mip, z, thr_binarize, w_connect, thr_filter, w_dilate):
    super(). __init__(cv, dst_cv, patch_bbox, overlap, mip, z, thr_binarize, w_connect, thr_filter, w_dilate)

  def execute(self, aligner):
    cv = DCV(self.cv)
    dst_cv = DCV(self.dst_cv)
    z = self.z
    mip = self.mip
    overlap = self.overlap
    overlap_bbox = np.array(overlap)*(2**mip)
    
    patch_bbox_in = deserialize_bbox(self.patch_bbox)
    patch_bbox_in.extend(overlap_bbox)
    patch_range = patch_bbox_in.range(mip)
    patch_bbox_out = deserialize_bbox(self.patch_bbox)
    patch_size = patch_bbox_out.size(mip)

    thr_binarize = self.thr_binarize
    w_connect = self.w_connect
    thr_filter = self.thr_filter
    w_dilate = self.w_dilate

    # length_filter = self.length_filter
    # if length_filter:
    #   small_dst_cv = DCV(self.small_dst_cv)
    #   medium_dst_cv = DCV(self.medium_dst_cv)
    #   large_dst_cv = DCV(self.large_dst_cv)
    #   medium_length_threshold = self.medium_length_threshold
    #   large_length_threshold = self.large_length_threshold
    
    print("\nFold detection postprocess "
          "cv {}\n"
          "z={}\n"
          "at MIP{}"
          "\n".format(cv, z, mip), flush=True)

    start = time()
    # if length_filter:
      # assert(medium_length_threshold < large_length_threshold)
      # small_fold_image, medium_fold_image, large_fold_image = aligner.fold_postprocess_chunk(cv, patch_bbox_in, z, mip, thr_binarize, w_connect, thr_filter, w_dilate, 
      #                                        length_filter, medium_length_threshold, large_length_threshold)
      # aligner.save_image(small_fold_image, small_dst_cv, z, patch_bbox_out, mip, to_uint8=True)
      # aligner.save_image(medium_fold_image, medium_dst_cv, z, patch_bbox_out, mip, to_uint8=True)
      # aligner.save_image(large_fold_image, large_dst_cv, z, patch_bbox_out, mip, to_uint8=True)
      # pass
    # else:
    image = aligner.fold_postprocess_chunk(cv, patch_bbox_in, z, mip, thr_binarize, w_connect, thr_filter, w_dilate)
    image = image[np.newaxis,np.newaxis,...]
    min_bound = cv[mip].bounds.minpt
    image = image[(slice(0,1),slice(0,1),)+
                  tuple([slice(overlap[i]*(patch_range[i][0]>min_bound[i]),
                    overlap[i]*(patch_range[i][0]>min_bound[i])+patch_size[i]) for i in [0,1]])]
    aligner.save_image(image, dst_cv, z, patch_bbox_out, mip, to_uint8=True)
    end = time()
    diff = end - start
    print('Fold detection postprocess task: {:.3f} s'.format(diff))

class MaskLogicTask(RegisteredTask):
  def __init__(self, cv_list, dst_cv, z_list, dst_z, bbox, mip_list, dst_mip, op):
    super(). __init__(cv_list, dst_cv, z_list, dst_z, bbox, mip_list, dst_mip, op)

  def execute(self, aligner):
    cv_list = [DCV(f) for f in self.cv_list]
    dst = DCV(self.dst_cv)
    z_list = self.z_list
    dst_z = self.dst_z
    patch_bbox = deserialize_bbox(self.bbox)
    mip_list = self.mip_list
    dst_mip = self.dst_mip
    op = self.op
    print("\nMaskLogicTask\n"
          "op {}\n"
          "cv_list {}\n"
          "dst {}\n"
          "z_list {}\n"
          "dst_z {}\n"
          "mip_list {}\n"
          "dst_mip {}\n"
          .format(op, cv_list, dst, z_list, dst_z, mip_list, dst_mip),
          flush=True)
    start = time()
    if op == 'and':
      res = aligner.mask_conjunction_chunk(cv_list, z_list, patch_bbox, mip_list,
                                           dst_mip)
    elif op == 'or':
      res = aligner.mask_disjunction_chunk(cv_list, z_list, patch_bbox, mip_list,
                                           dst_mip)

    aligner.save_image(res, dst, dst_z, patch_bbox, dst_mip, to_uint8=True)
    end = time()
    diff = end - start
    print('Task: {:.3f} s'.format(diff))

class CopyTask(RegisteredTask):
  def __init__(self, src_cv, dst_cv, src_z, dst_z, patch_bbox, mip, 
               is_field, mask_cv, mask_mip, mask_val, prefix):
    super().__init__(src_cv, dst_cv, src_z, dst_z, patch_bbox, mip, 
                     is_field, mask_cv, mask_mip, mask_val, prefix)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv)
    dst_cv = DCV(self.dst_cv)
    src_z = self.src_z
    dst_z = self.dst_z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    mip = self.mip
    is_field = self.is_field
    mask_cv = None 
    if self.mask_cv:
      mask_cv = DCV(self.mask_cv)
    mask_mip = self.mask_mip
    mask_val = self.mask_val
    prefix = self.prefix
    print("\nCopy\n"
          "src {}\n"
          "dst {}\n"
          "mask {}, val {}, MIP{}\n"
          "z={} to z={}\n"
          "MIP{}\n".format(src_cv, dst_cv, mask_cv, mask_val, mask_mip, 
                            src_z, dst_z, mip), flush=True)
    start = time()
    if not aligner.dry_run:
      if is_field:
        field =  aligner.get_field(src_cv, src_z, patch_bbox, mip, relative=False,
                                to_tensor=False)
        aligner.save_field(field, dst_cv, dst_z, patch_bbox, mip, relative=False)
      else:
        image = aligner.get_masked_image(src_cv, src_z, patch_bbox, mip,
                                mask_cv=mask_cv, mask_mip=mask_mip,
                                mask_val=mask_val,
                                to_tensor=False, normalizer=None)
        aligner.save_image(image, dst_cv, dst_z, patch_bbox, mip)
      with Storage(dst_cv.path) as stor:
          path = 'copy_done/{}/{}'.format(prefix, patch_bbox.stringify(dst_z))
          stor.put_file(path, '')
          print('Marked finished at {}'.format(path))
      end = time()
      diff = end - start
      print(':{:.3f} s'.format(diff))

class ComputeFieldTask(RegisteredTask):
  def __init__(self, model_path, src_cv, tgt_cv, field_cv, src_z, tgt_z, 
                     patch_bbox, mip, pad, src_mask_cv, src_mask_val, src_mask_mip, 
                     tgt_mask_cv, tgt_mask_val, tgt_mask_mip, prefix,
                     prev_field_cv, prev_field_z):
    super().__init__(model_path, src_cv, tgt_cv, field_cv, src_z, tgt_z, 
                     patch_bbox, mip, pad, src_mask_cv, src_mask_val, src_mask_mip, 
                     tgt_mask_cv, tgt_mask_val, tgt_mask_mip, prefix,
                     prev_field_cv, prev_field_z)

  def execute(self, aligner):
    model_path = self.model_path
    src_cv = DCV(self.src_cv) 
    tgt_cv = DCV(self.tgt_cv) 
    field_cv = DCV(self.field_cv)
    if self.prev_field_cv is not None:
        prev_field_cv = DCV(self.prev_field_cv)
    else:
        prev_field_cv = None
    src_z = self.src_z
    tgt_z = self.tgt_z
    prev_field_z = self.prev_field_z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    mip = self.mip
    pad = self.pad
    src_mask_cv = None 
    if self.src_mask_cv:
      src_mask_cv = DCV(self.src_mask_cv)
    src_mask_mip = self.src_mask_mip
    src_mask_val = self.src_mask_val
    tgt_mask_cv = None 
    if self.tgt_mask_cv:
      tgt_mask_cv = DCV(self.tgt_mask_cv)
    tgt_mask_mip = self.tgt_mask_mip
    tgt_mask_val = self.tgt_mask_val
    prefix = self.prefix
    print("\nCompute field\n"
          "model {}\n"
          "src {}\n"
          "tgt {}\n"
          "field {}\n"
          "src_mask {}, val {}, MIP{}\n"
          "tgt_mask {}, val {}, MIP{}\n"
          "z={} to z={}\n"
          "MIP{}\n".format(model_path, src_cv, tgt_cv, field_cv, src_mask_cv, src_mask_val,
                           src_mask_mip, tgt_mask_cv, tgt_mask_val, tgt_mask_mip, 
                           src_z, tgt_z, mip), flush=True)
    start = time()
    if not aligner.dry_run:
      field = aligner.compute_field_chunk(model_path, src_cv, tgt_cv, src_z, tgt_z, 
                                          patch_bbox, mip, pad, 
                                          src_mask_cv, src_mask_mip, src_mask_val,
                                          tgt_mask_cv, tgt_mask_mip, tgt_mask_val,
                                          None, prev_field_cv, prev_field_z)
      aligner.save_field(field, field_cv, src_z, patch_bbox, mip, relative=False)
      with Storage(field_cv.path) as stor:
        path = 'compute_field_done/{}/{}'.format(prefix, patch_bbox.stringify(src_z))
        stor.put_file(path, '')
        print('Marked finished at {}'.format(path))
      end = time()
      diff = end - start
      print('ComputeFieldTask: {:.3f} s'.format(diff))

class RenderTask(RegisteredTask):
  def __init__(self, src_cv, field_cv, dst_cv, src_z, field_z, dst_z, patch_bbox, src_mip,
               field_mip, mask_cv, mask_mip, mask_val, affine, prefix, use_cpu=False):
    super(). __init__(src_cv, field_cv, dst_cv, src_z, field_z, dst_z, patch_bbox, src_mip, 
                     field_mip, mask_cv, mask_mip, mask_val, affine, prefix, use_cpu)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv) 
    field_cv = DCV(self.field_cv) 
    dst_cv = DCV(self.dst_cv) 
    src_z = self.src_z
    field_z = self.field_z
    dst_z = self.dst_z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    src_mip = self.src_mip
    field_mip = self.field_mip
    mask_cv = None 
    if self.mask_cv:
      mask_cv = DCV(self.mask_cv)
    mask_mip = self.mask_mip
    mask_val = self.mask_val
    affine = None 
    if self.affine:
      affine = np.array(self.affine)
    prefix = self.prefix
    print("\nRendering\n"
          "src {}\n"
          "field {}\n"
          "dst {}\n"
          "z={} to z={}\n"
          "MIP{} to MIP{}\n"
          "Preconditioning affine\n"
          "{}\n".format(src_cv, field_cv, dst_cv, src_z, dst_z, 
                        field_mip, src_mip, affine), flush=True)
    start = time()
    if not aligner.dry_run:
      image = aligner.cloudsample_image(src_cv, field_cv, src_z, field_z,
                                     patch_bbox, src_mip, field_mip,
                                     mask_cv=mask_cv, mask_mip=mask_mip,
                                     mask_val=mask_val, affine=affine,
                                     use_cpu=self.use_cpu)
      image = image.cpu().numpy()
      aligner.save_image(image, dst_cv, dst_z, patch_bbox, src_mip)
      with Storage(dst_cv.path) as stor:
        path = 'render_done/{}/{}'.format(prefix, patch_bbox.stringify(dst_z))
        stor.put_file(path, '')
        print('Marked finished at {}'.format(path))
      end = time()
      diff = end - start
      print('RenderTask: {:.3f} s'.format(diff))

class VectorVoteTask(RegisteredTask):
  def __init__(self, pairwise_cvs, vvote_cv, z, patch_bbox, mip, inverse, serial, prefix):
    super().__init__(pairwise_cvs, vvote_cv, z, patch_bbox, mip, inverse, serial, prefix)

  def execute(self, aligner):
    pairwise_cvs = {int(k): DCV(v) for k,v in self.pairwise_cvs.items()}
    vvote_cv = DCV(self.vvote_cv)
    z = self.z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    mip = self.mip
    inverse = bool(self.inverse)
    serial = bool(self.serial)
    prefix = self.prefix
    print("\nVector vote\n"
          "fields {}\n"
          "dst {}\n"
          "z={}\n"
          "MIP{}\n"
          "inverse={}\n"
          "serial={}\n".format(pairwise_cvs.keys(), vvote_cv, z, 
                              mip, inverse, serial), flush=True)
    start = time()
    if not aligner.dry_run:
      field = aligner.vector_vote_chunk(pairwise_cvs, vvote_cv, z, patch_bbox, mip, 
                       inverse=inverse, serial=serial)
      field = field.data.cpu().numpy()
      aligner.save_field(field, vvote_cv, z, patch_bbox, mip, relative=False)
      with Storage(vvote_cv.path) as stor:
        path = 'vector_vote_done/{}/{}'.format(prefix, patch_bbox.stringify(z))
        stor.put_file(path, '')
        print('Marked finished at {}'.format(path))
      end = time()
      diff = end - start
      print('VectorVoteTask: {:.3f} s'.format(diff))

class ComposeTask(RegisteredTask):
  def __init__(self, f_cv, g_cv, dst_cv, f_z, g_z, dst_z, patch_bbox, f_mip, g_mip, 
                     dst_mip, factor, prefix):
    super().__init__(f_cv, g_cv, dst_cv, f_z, g_z, dst_z, patch_bbox, f_mip, g_mip, 
                     dst_mip, factor, prefix)

  def execute(self, aligner):
    f_cv = DCV(self.f_cv)
    g_cv = DCV(self.g_cv)
    dst_cv = DCV(self.dst_cv)
    f_z = self.f_z
    g_z = self.g_z
    dst_z = self.dst_z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    f_mip = self.f_mip
    g_mip = self.g_mip
    dst_mip = self.dst_mip
    factor = self.factor
    prefix = self.prefix
    print("\nCompose\n"
          "f {}\n"
          "g {}\n"
          "f_z={}, g_z={}\n"
          "f_MIP{}, g_MIP{}\n"
          "dst {}\n"
          "dst_MIP {}\n"
          "factor={}\n".format(f_cv, g_cv, f_z, g_z, f_mip, g_mip, dst_cv, 
                               dst_mip, factor), flush=True)
    start = time()
    if not aligner.dry_run:
      h = aligner.get_composed_field(f_cv, g_cv, f_z, g_z, patch_bbox, 
                                   f_mip, g_mip, dst_mip, factor)
      h = h.data.cpu().numpy()
      aligner.save_field(h, dst_cv, dst_z, patch_bbox, dst_mip, relative=False)
      with Storage(dst_cv.path) as stor:
        path = 'compose_done/{}/{}'.format(prefix, patch_bbox.stringify(dst_z))
        stor.put_file(path, '')
        print('Marked finished at {}'.format(path))
      end = time()
      diff = end - start
      print('ComposeTask: {:.3f} s'.format(diff))

class CPCTask(RegisteredTask):
  def __init__(self, src_cv, tgt_cv, dst_cv, src_z, tgt_z, patch_bbox, 
                    src_mip, dst_mip, norm, prefix):
    super().__init__(src_cv, tgt_cv, dst_cv, src_z, tgt_z, patch_bbox, 
                    src_mip, dst_mip, norm, prefix)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv) 
    tgt_cv = DCV(self.tgt_cv) 
    dst_cv = DCV(self.dst_cv)
    src_z = self.src_z
    tgt_z = self.tgt_z
    patch_bbox = deserialize_bbox(self.patch_bbox)
    src_mip = self.src_mip
    dst_mip = self.dst_mip
    norm = self.norm
    prefix = self.prefix
    print("\nCPC\n"
          "src {}\n"
          "tgt {}\n"
          "src_z={}, tgt_z={}\n"
          "src_MIP{} to dst_MIP{}\n"
          "norm={}\n"
          "dst {}\n".format(src_cv, tgt_cv, src_z, tgt_z, src_mip, dst_mip, norm,
                            dst_cv), flush=True)
    if not aligner.dry_run:
      r = aligner.cpc_chunk(src_cv, tgt_cv, src_z, tgt_z, patch_bbox, src_mip, 
                            dst_mip, norm)
      r = r.cpu().numpy()
      aligner.save_image(r, dst_cv, src_z, patch_bbox, dst_mip, to_uint8=norm)
      with Storage(dst_cv.path) as stor:
        path = 'cpc_done/{}/{}'.format(prefix, patch_bbox.stringify(src_z))
        stor.put_file(path, '')
        print('Marked finished at {}'.format(path))

class BatchRenderTask(RegisteredTask):
  def __init__(
    self, z, field_cv, field_z, patches, 
    mip, dst_cv, dst_z, batch
  ):
    super().__init__(
      z, field_cv, field_z, patches, 
      mip, dst_cv, dst_z, batch
    )
    #self.patches = [p.serialize() for p in patches]

  def execute(self, aligner):
    src_z = self.z
    patches  = [deserialize_bbox(p) for p in self.patches]
    batch = self.batch
    field_cv = DCV(self.field_cv)
    mip = self.mip
    field_z = self.field_z
    dst_cv = DCV(self.dst_cv)
    dst_z = self.dst_z

    def chunkwise(patch_bbox):
      print ("Rendering {} at mip {}".format(patch_bbox.__str__(mip=0), mip),
              end='', flush=True)
      warped_patch = aligner.warp_patch_batch(src_z, field_cv, field_z,
                                           patch_bbox, mip, batch)
      aligner.save_image_patch_batch(dst_cv, (dst_z, dst_z + batch),
                                  warped_patch, patch_bbox, mip)
      with Storage(dst_cv.path) as stor:
          stor.put_file('render_batch/'+str(mip)+'_'+str(dst_z)+'_'+str(batch)+'/'+ patch_bbox.__str__(), '')
    aligner.pool.map(chunkwise, patches)

class DownsampleTask(RegisteredTask):
  def __init__(self, cv, z, patches, mip):
    super().__init__(cv, z, patches, mip)
    #self.patches = [p.serialize() for p in patches]

  def execute(self, aligner):
    z = self.z
    cv = DCV(self.cv)
    #patches  = deserialize_bbox(self.patches)
    patches  = [deserialize_bbox(p) for p in self.patches]
    mip = self.mip
    #downsampled_patch = aligner.downsample_patch(cv, z, patches, mip - 1)
    #aligner.save_image_patch(cv, z, downsampled_patch, patches, mip)
    def chunkwise(patch_bbox):
      downsampled_patch = aligner.downsample_patch(cv, z, patch_bbox, mip - 1)
      aligner.save_image_patch(cv, z, downsampled_patch, patch_bbox, mip)
      with Storage(cv.path) as stor:
          stor.put_file('downsample_done/'+str(mip)+'_'+str(z)+'/'+patch_bbox.__str__(), '')
    aligner.pool.map(chunkwise, patches)

class InvertFieldTask(RegisteredTask):
  def __init__(self, z, src_cv, dst_cv, patch_bbox, mip, optimizer):
    super().__init__(z, src_cv, dst_cv, patch_bbox, mip, optimizer)

  def execute(self, aligner):
    src_cv = DCV(self.src_cv)
    dst_cv = DCV(self.dst_cv)
    patch_bbox = deserialize_bbox(self.patch_bbox)

    aligner.invert_field(
      self.z, src_cv, dst_cv,
      patch_bbox, self.mip, self.optimizer
    )

class PrepareTask(RegisteredTask):
  def __init__(self, z, patches, mip, start_z):
    super().__init__(z, patches, mip, start_z)
    #self.patches = [ p.serialize() for p in patches ]

  def execute(self, aligner):
    patches = [ deserialize_bbox(p) for p in self.patches ]

    def chunkwise(patch_bbox):
      print("Preparing source {} at mip {}".format(
        patch_bbox.__str__(mip=0), mip
      ), end='', flush=True)

      warped_patch = aligner.warp_patch(
        aligner.src_ng_path, self.z, patch_bbox,
        (self.mip, aligner.process_high_mip), 
        self.mip, self.start_z
      )
      aligner.save_image_patch(
        aligner.tmp_ng_path, warped_patch, self.z, patch_bbox, self.mip
      )

    aligner.pool.map(chunkwise, patches)    

class RegularizeTask(RegisteredTask):
  def __init__(self, z_start, z_end, compose_start, patch_bbox, mip, sigma):
    super().__init(z_start, z_end, compose_start, patch_bbox, mip, sigma)

  def execute(self, aligner):
    patch_bbox = deserialize_bbox(self.patch_bbox)
    z_range = range(self.z_start, self.z_end+1)
    
    aligner.regularize_z(
      z_range, self.compose_start, 
      patch_bbox, self.mip, 
      sigma=self.sigma
    )    

class RenderCVTask(RegisteredTask):
  def __init__(self, z, field_cv, field_z, patches, mip, dst_cv, dst_z):
    super().__init__(z, field_cv, field_z, patches, mip, dst_cv, dst_z)
    #self.patches = [p.serialize() for p in patches]

  def execute(self, aligner):
    src_z = self.z
    patches  = [deserialize_bbox(p) for p in self.patches]
    #patches  = deserialize_bbox(self.patches)
    field_cv = DCV(self.field_cv) 
    mip = self.mip
    field_z = self.field_z
    dst_cv = DCV(self.dst_cv)
    dst_z = self.dst_z

    def chunkwise(patch_bbox):
      print ("Rendering {} at mip {}".format(patch_bbox.__str__(mip=0), mip),
              end='', flush=True)
      warped_patch = aligner.warp_using_gridsample_cv(src_z, field_cv, field_z, patch_bbox, mip)
      aligner.save_image_patch(dst_cv, dst_z, warped_patch, patch_bbox, mip)
      with Storage(dst_cv.path) as stor:
          stor.put_file('render_cv/'+str(mip)+'_'+str(dst_z)+'/'+ patch_bbox.__str__(), '')
    aligner.pool.map(chunkwise, patches)    

class RenderLowMipTask(RegisteredTask):
  def __init__(
    self, z, field_cv, field_z, patches, 
    image_mip, vector_mip, dst_cv, dst_z
  ):
    super().__init__(
      z, field_cv, field_z, patches, 
      image_mip, vector_mip, dst_cv, dst_z
    )
    #self.patches = [p.serialize() for p in patches]

  def execute(self, aligner):
    src_z = self.z
    patches  = [deserialize_bbox(p) for p in self.patches]
    field_cv = DCV(self.field_cv) 
    image_mip = self.image_mip
    vector_mip = self.vector_mip
    field_z = self.field_z
    dst_cv = DCV(self.dst_cv)
    dst_z = self.dst_z
    def chunkwise(patch_bbox):
      print ("Rendering {} at mip {}".format(patch_bbox.__str__(mip=0), image_mip),
              end='', flush=True)
      warped_patch = aligner.warp_patch_at_low_mip(src_z, field_cv, field_z, 
                                                patch_bbox, image_mip, vector_mip)
      aligner.save_image_patch(dst_cv, dst_z, warped_patch, patch_bbox, image_mip)
      with Storage(dst_cv.path) as stor:
          stor.put_file('render_low_mip/'+str(image_mip)+'_'+str(dst_z)+'/'+ patch_bbox.__str__(), '')
    aligner.pool.map(chunkwise, patches)

class ResAndComposeTask(RegisteredTask):
  def __init__(self, model_path, src_cv, tgt_cv, z, tgt_range, patch_bbox, mip,
               w_cv, pad, softmin_temp, prefix):
    super().__init__(model_path, src_cv, tgt_cv, z, tgt_range, patch_bbox, mip,
               w_cv, pad, softmin_temp, prefix)

  def execute(self, aligner):
    patch_bbox = deserialize_bbox(self.patch_bbox)
    w_cv = DCV(self.w_cv)
    src_cv = DCV(self.src_cv)
    tgt_cv = DCV(self.tgt_cv)
    print("self tgt_range is", self.tgt_range)
    aligner.res_and_compose(self.model_path, src_cv, tgt_cv, self.z,
                            self.tgt_range, patch_bbox, self.mip, w_cv,
                            self.pad, self.softmin_temp)
    with Storage(w_cv.path) as stor:
      path = 'res_and_compose/{}-{}/{}'.format(self.prefix, self.mip,
                                               patch_bbox.stringify(self.z))
      stor.put_file(path, '')
      print('Marked finished at {}'.format(path))

class UpsampleRenderRechunkTask(RegisteredTask):
  def __init__(
    self, z_range, src_cv, field_cv, dst_cv, 
    patches, image_mip, field_mip
  ):
    super().__init__(
      z_range, src_cv, field_cv, dst_cv, 
      patches, image_mip, field_mip
    )
    #self.patches = [p.serialize() for p in patches]

  def execute(self, aligner):
    z_start = self.z_start
    z_end = self.z_end
    patches  = [deserialize_bbox(p) for p in self.patches]
    #patches  = deserialize_bbox(self.patches)
    src_cv = DCV(self.src_cv) 
    field_cv = DCV(self.field_cv) 
    dst_cv = DCV(self.dst_cv)
    image_mip = self.image_mip
    field_mip = self.field_mip
    z_range = range(z_start, z_end+1)
    def chunkwise(patch_bbox):
      warped_patch = aligner.warp_gridsample_cv_batch(z_range, src_cv, field_cv, 
                                                   patch_bbox, image_mip, field_mip)
      print('warped_patch.shape {0}'.format(warped_patch.shape))
      aligner.save_image_patch_batch(dst_cv, (z_range[0], z_range[-1]+1), warped_patch, 
                                  patch_bbox, image_mip)
    aligner.pool.map(chunkwise, patches)

class ComputeFcorrTask(RegisteredTask):
  def __init__(self, cv, dst_cv, dst_nopost, patch_bbox, mip, z1, z2, prefix):
    super(). __init__(cv, dst_cv, dst_nopost, patch_bbox, mip, z1, z2, prefix)

  def execute(self, aligner):
    cv = DCV(self.cv)
    dst_cv = DCV(self.dst_cv)
    dst_nopost = DCV(self.dst_nopost)
    z1 = self.z1
    z2 = self.z2
    patch_bbox = deserialize_bbox(self.patch_bbox)
    mip = self.mip
    print("\nFcorring "
          "cv {}\n"
          "z={} to z={}\n"
          "at MIP{}"
          "\n".format(cv, z1, z2, mip), flush=True)
    start = time()
    image, image_no = aligner.get_fcorr(patch_bbox, cv, mip, z1, z2)
    aligner.save_image(image, dst_cv, z2, patch_bbox, 8, to_uint8=False)
    aligner.save_image(image_no, dst_nopost, z2, patch_bbox, 8, to_uint8=False)
    with Storage(dst_cv.path) as stor:
      path = 'Fcorr_done/{}/{}'.format(self.prefix, patch_bbox.stringify(z2))
      stor.put_file(path, '')
      print('Marked finished at {}'.format(path))
    end = time()
    diff = end - start
    print('FcorrTask: {:.3f} s'.format(diff))
