import os.path as osp
from torch import nn

my_path = '/root/path/to/file.log.txt'

print(osp.basename(my_path))
print(osp.splitext(my_path))
print(osp.basename(my_path))