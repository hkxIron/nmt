#!/bin/bash
common_path="data/nmt"
model_path="$common_path/nmt_model"
data_path="$common_path/nmt_data"
[[ ! -d "$model_path" ]]&&{ mkdir -p $model_path; }
download_data=0
[[ "$1" != "" ]]&&{ download_data=$1; }
if [[ $download_data -eq 1 ]]; then
    nmt/scripts/download_iwslt15.sh $data_path
fi

<<EOF
EOF
# source activate python36
set -x
python -m nmt.nmt \
    --src=vi --tgt=en \
    --vocab_prefix=$data_path/vocab  \
    --train_prefix=$data_path/train \
    --dev_prefix=$data_path/tst2012  \
    --test_prefix=$data_path/tst2013 \
    --out_dir=$model_path \
    --num_train_steps=12000 \
    --steps_per_stats=100 \
    --num_layers=2 \
    --num_units=128 \
    --dropout=0.2 \
    --metrics=bleu \
    --batch_size=32
