import sys
import torch
from args import get_argparser, parse_args, get_aligner, get_bbox
from os.path import join
from cloudvolume import CloudVolume

if __name__ == '__main__':
  parser = get_argparser()
  parser.add_argument('--align_start',
    help='align without vector voting the 2nd & 3rd sections, otherwise copy them', action='store_true')
  args = parse_args(parser)
  args.tgt_path = join(args.dst_path, 'image')
  # only compute matches to previous sections
  args.serial_operation = True
  a = get_aligner(args)
  bbox = get_bbox(args)

  #a.add_path('new_dst_img', join(args.new_dst_path, 'image'), data_type='uint8', num_channels=1, fill_missing=True)
  z_range = range(args.bbox_start[2], args.bbox_stop[2])
  a.dst[0].add_composed_cv(args.bbox_start[2]+1, inverse=False, use_int=a.int_field)
  field_k = a.dst[0].get_composed_key(args.bbox_start[2]+1, inverse=False)
  field_cv= a.dst[0].for_read(field_k)
  dst_cv = a.dst[0].for_write('dst_img1')
  #new_dst_cv = a.dst[0].for_write('new_dst_img')
  z_offset = 1
  uncomposed_field_cv = a.dst[z_offset].for_read('field')

  mip = args.mip
  composed_range = z_range[3:4]
  #copy_range = z_range[0:1]
  #composed_range = z_range[0:1]
  if args.align_start:
    copy_range = z_range[0:1]
    uncomposed_range = z_range[1:3]
  else:
    copy_range = z_range[0:3]
    uncomposed_range = z_range[0:0]
  #uncomposed_range = z_range[0:1]

  # copy first section
#  for z in copy_range:
#    print('Copying z={0}'.format(z))
#    a.copy_section(z, dst_cv, z, bbox, mip)
#    a.downsample(dst_cv, z, bbox, a.render_low_mip, a.render_high_mip)
#  # align without vector voting
#  for z in uncomposed_range:
#    #z +=1
#    print('compute residuals without vector voting z={0}'.format(z))
#    src_z = z
#    tgt_z = z-1
#    a.compute_section_pair_residuals(src_z, tgt_z, bbox)
#  #  a.render_section_all_mips(src_z, uncomposed_field_cv, src_z,
#  #                            dst_cv, src_z, bbox, mip) 
#    a.render_grid_cv(src_z, uncomposed_field_cv, src_z, dst_cv, src_z, bbox, a.render_low_mip)
#    #a.render(src_z, uncomposed_field_cv, src_z, dst_cv, src_z, bbox, a.render_low_mip)
#    a.downsample(dst_cv, src_z, bbox, a.render_low_mip, a.render_high_mip)
#
#a.render_section_all_mips(z, field_cv, z, dst_cv, z, bbox, mip)
  # align with vector voting

  for z in composed_range:
      #print('generate pairwise with vector voting z={0}'.format(z))
      #a.generate_pairwise([z], bbox, forward_match=True, reverse_match=False,
      #                    render_match=False)
      #print('compose pairwise with vector voting z={0}'.format(z))
      #a.compose_pairwise([z], args.bbox_start[2], bbox, mip, forward_compose=True,
      #                   inverse_compose=False)
      a.generate_pairwise_and_compose([z], args.bbox_start[2]+1, bbox,
                                      mip, forward_match=True,
                                      reverse_match=False)
      src_z = z
      print('Aligning with vector voting z={0}'.format(z))
      a.render(src_z, field_cv, src_z, dst_cv, src_z, bbox, a.render_low_mip)
      print('Downsample z={0}'.format(z))
      a.downsample(dst_cv, src_z, bbox, a.render_low_mip, a.render_high_mip) 

#  field_cv[mip].info['data_type'] = 'uint16'
#  field_cv[mip].commit_info()
#  uncomposed_field_cv[mip].info['data_type'] = 'uint16'
#  uncomposed_field_cv[mip].commit_info()


