from aligner import Aligner, BoundingBox
from copy import deepcopy
from time import time
s = 512
t = 5120
mip = 7
source_z = 11
target_z = 10
v_off = (10240, 4096, 0)

patch_bbox = BoundingBox(v_off[0]+t, v_off[0]+t+s, v_off[1]+t, v_off[1]+t+s, mip=0, max_mip=9)
patch_bbox = BoundingBox(v_off[0], v_off[0]+57344, v_off[1], v_off[1]+40960, mip=0, max_mip=9)
crop = 64
influence_bbox = deepcopy(patch_bbox)
influence_bbox.uncrop(crop, mip=mip)

a = Aligner('model/2_5_2.pt', (64, 64), 1024, 32, 9, 7, 'gs://neuroglancer/pinky40_alignment/prealigned', 'gs://neuroglancer/nflow_tests/p40_pre', move_anchor=False)

for z in range(10, 100):
  a.set_processing_chunk_size((64, 64))
  start = time()
  a.align_ng_stack(z, z + 1, patch_bbox)
  end = time()
  print ("Aligmnent: {} sec".format(end-start))
  c = 2048
  m = 2
  start = time()
  for x in range(0, 6):
    s = max(int(2048/(2**x)), 64)
    a.set_processing_chunk_size((s, s))
    a.render(z + 1, patch_bbox, mip=m+x)
  end = time()
  print ("Render: {} sec".format(end-start))
#a.compute_section_pair_residuals(source_z, target_z, patch_bbox)
#a.save_aggregate_flow(source_z, patch_bbox, mip=6)
#a.save_aggregate_flow(source_z, patch_bbox, mip=5)
#a.save_aggregate_flow(source_z, patch_bbox, mip=4)
#a.render(source_z, patch_bbox, mip=9)
#a.render(source_z, patch_bbox, mip=8)
#a.render(source_z, patch_bbox, mip=2)
#a.render(source_z, patch_bbox, mip=7)
