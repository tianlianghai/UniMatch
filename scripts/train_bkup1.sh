#!/bin/bash
now=$(date +"%Y%m%d_%H%M%S")

# modify these augments if you want to try other datasets, splits or methods
# dataset: ['pascal', 'cityscapes', 'coco']
# method: ['unimatch', 'fixmatch', 'supervised']
# exp: just for specifying the 'save_path'
# split: ['92', '1_16', 'u2pl_1_16', ...]. Please check directory './splits/$dataset' for concrete splits
# dataset='pascal'
dataset='cityscapes'
method='unimatch'
exp='r50'
# split='732'
split='1_16'

config=configs/${dataset}.yaml
labeled_id_path=splits/$dataset/$split/labeled.txt
unlabeled_id_path=splits/$dataset/$split/unlabeled.txt
save_path=exp/$dataset/$method/$exp/$split

mkdir -p $save_path
echo "save path is $save_path"

# python \
#     $method.py \
#     --config=$config --labeled-id-path $labeled_id_path --unlabeled-id-path $unlabeled_id_path \
#     --save-path $save_path  ${@:2} 2>&1 | tee $save_path/$now.log


accelerate launch \
    $method.py \
    --config=$config --labeled-id-path $labeled_id_path --unlabeled-id-path $unlabeled_id_path \
    --save-path $save_path  ${@} 2>&1 | tee $save_path/$now.log