import gevent.monkey
gevent.monkey.patch_all()

from concurrent.futures import ProcessPoolExecutor
import taskqueue
from taskqueue import TaskQueue, GreenTaskQueue, LocalTaskQueue

import sys
import torch
import json
from args import get_argparser, parse_args, get_aligner, get_bbox, get_provenance
from os.path import join
from cloudmanager import CloudManager
from time import time
from tasks import run 

def print_run(diff, n_tasks):
  if n_tasks > 0:
    print (": {:.3f} s, {} tasks, {:.3f} s/tasks".format(diff, n_tasks, diff / n_tasks))

def make_range(block_range, part_num):
    rangelen = len(block_range)
    if(rangelen < part_num):
        srange =1
        part = rangelen
    else:
        part = part_num
        srange = rangelen//part
    range_list = []
    for i in range(part-1):
        range_list.append(block_range[i*srange:(i+1)*srange])
    range_list.append(block_range[(part-1)*srange:])
    return range_list

if __name__ == '__main__':
  parser = get_argparser()
  parser.add_argument('--src_path', type=str)
  parser.add_argument('--dst_path', type=str)
  parser.add_argument('--mip', type=int)
  parser.add_argument('--frequency', type=float)
  parser.add_argument('--max_mip', type=int)
  parser.add_argument('--bbox_start', nargs=3, type=int,
    help='bbox origin, 3-element int list')
  parser.add_argument('--bbox_stop', nargs=3, type=int,
    help='bbox origin+shape, 3-element int list')
  parser.add_argument('--bbox_mip', type=int, default=0,
    help='MIP level at which bbox_start & bbox_stop are specified')
  parser.add_argument('--pad', 
    help='the size of the largest displacement expected; should be 2^high_mip', 
    type=int, default=2048)
  # parser.add_argument('--save_intermediary', action='store_true')
  args = parse_args(parser)
  a = get_aligner(args)
  a.chunk_size = (1024, 1024) 
  bbox = get_bbox(args)
  provenance = get_provenance(args)
  
  # Simplify var names
  mip = args.mip
  max_mip = args.max_mip
  pad = args.pad
  frequency = args.frequency
  print('mip {}'.format(mip))
  print('frequency {}'.format(frequency))

  # Compile ranges
  full_range = range(args.bbox_start[2], args.bbox_stop[2])
  # Create CloudVolume Manager
  cm = CloudManager(args.src_path, 12, pad, provenance, batch_size=1,
                    size_chunk=1, batch_mip=12)

  # Create src CloudVolumes
  src = cm.create(args.src_path, data_type='uint8', num_channels=1,
                     fill_missing=True, overwrite=False).path

  seam_dir = 'seams/{}'.format(mip)
  # Create dst CloudVolumes
  dst_pre = cm.create(join(args.dst_path, seam_dir, 'pre'),
                  data_type='uint8', num_channels=1, fill_missing=True,
                  overwrite=True).path
  dst_post = cm.create(join(args.dst_path, seam_dir, 'post'),
                  data_type='float32', num_channels=1, fill_missing=True,
                  overwrite=True).path

  class TaskIterator():
      def __init__(self, brange):
          self.brange = brange
      def __iter__(self):
          for z in self.brange:
            t = a.find_seams(cm, src, dst_pre, dst_post, z, z, bbox, mip, frequency) 
            yield from t

  range_list = make_range(full_range, a.threads)

  def remote_upload(tasks):
    with GreenTaskQueue(queue_name=args.queue_name) as tq:
        tq.insert_all(tasks)

  start = time()
  ptask = []
  for i in range_list:
      ptask.append(TaskIterator(i))

  if a.distributed:
    with ProcessPoolExecutor(max_workers=a.threads) as executor:
        executor.map(remote_upload, ptask)
  else:
      for t in ptask:
        tq = LocalTaskQueue(parallel=1)
        tq.insert_all(t, args= [a])


  end = time()
  diff = end - start
  print("Sending Tasks use time:", diff)
  print('Running Tasks')
