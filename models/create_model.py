#/usr/bin/python3
import os
import argparse
import subprocess
from subprocess import Popen, PIPE, STDOUT

parser = argparse.ArgumentParser()
parser.add_argument('--weights_dir', type=str, default='/usr/people/popovych/alignment/nets/')
parser.add_argument('--template', type=str, default="sergiy_m8m10_template")
parser.add_argument('--name_template', type=str, default="zzzz_02_03_mip_10_z{}")
parser.add_argument('--prefix', type=str, default="sergiy_m8m10_")
parser.add_argument('--mips', type=list, default=[8, 10])
parser.add_argument('--section', type=int, default=None)
args = parser.parse_args()

if args.section == None:
    starts = range(8000, 28000, 1024)
else:
    starts = [args.section]

for z in starts:
    print ("z == {}".format(z))
    name = args.name_template.format(z)

    net_dir = "{}{}".format(args.prefix, name)
    src_weights_dir = args.weights_dir
    tgt_weights_dir = os.path.join(net_dir, "weights.pt")
    subprocess.Popen("cp -r {} {}".format(args.template, net_dir), shell=True)


    for m in [10]:
        file_name = "{}_module{}.pth.tar".format(name, m)
        file_path = os.path.join(src_weights_dir, file_name)
        tgt_path = os.path.join(tgt_weights_dir, "module{}.pth.tar".format(m))
        print ("cp {} {}".format(file_path, tgt_weights_dir))
        subprocess.Popen("cp {} {}".format(file_path, tgt_path), shell=True)

    for m in [8]:
        file_name = "serial_x1_module{}.pth.tar".format(m)
        file_path = os.path.join(src_weights_dir, file_name)
        tgt_path = os.path.join(tgt_weights_dir, "module{}.pth.tar".format(m))
        print ("cp {} {}".format(file_path, tgt_weights_dir))
        subprocess.Popen("cp {} {}".format(file_path, tgt_path), shell=True)



