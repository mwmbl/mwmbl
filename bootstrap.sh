#!/bin/bash -xe

sudo python3 -m pip uninstall numpy -y
sudo python3 -m pip uninstall numpy -y
sudo python3 -m pip uninstall numpy -y

sudo python3 -m pip install boto3==1.19.7 botocore==1.22.7 jusText==3.0.0 langdetect==1.0.9 \
    lxml==4.6.3 numpy==1.21.3 pandas==1.2.5 pyarrow==6.0.0 spacy==2.3.5 \
    warcio==1.7.4 zstandard==0.16.0

sudo python3 -m spacy download en_core_web_sm

echo "========================"
echo "Normal python pip freeze"
python3 -m pip freeze
