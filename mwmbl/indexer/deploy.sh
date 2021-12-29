cat hn-top-domains-filtered.py extract.py > runextract.py

aws s3 cp runextract.py s3://tinysearch/code/
aws s3 cp bootstrap.sh s3://tinysearch/code/


aws emr create-cluster \
    --applications Name=Spark Name=Zeppelin \
    --ec2-attributes '{"InstanceProfile":"EMR_EC2_DefaultRole","SubnetId":"subnet-03c33360c68f73a48"}' \
    --service-role EMR_DefaultRole \
    --enable-debugging \
    --release-label emr-5.33.1 \
    --log-uri 's3n://tinysearch/pyspark-logs/' \
    --bootstrap-actions '{"Path": "s3://tinysearch/code/bootstrap.sh"}' \
    --steps '[{"Args":["spark-submit","--deploy-mode","cluster","s3n://tinysearch/code/runextract.py"],"Type":"CUSTOM_JAR","ActionOnFailure":"CONTINUE","Jar":"command-runner.jar","Properties":"","Name":"Spark application"}]' \
    --name 'TinySearch' \
    --instance-groups '[{"InstanceCount":2,"EbsConfiguration":{"EbsBlockDeviceConfigs":[{"VolumeSpecification":{"SizeInGB":32,"VolumeType":"gp2"},"VolumesPerInstance":1}]},"InstanceGroupType":"CORE","InstanceType":"m4.large","Name":"Core Instance Group"},{"InstanceCount":1,"EbsConfiguration":{"EbsBlockDeviceConfigs":[{"VolumeSpecification":{"SizeInGB":32,"VolumeType":"gp2"},"VolumesPerInstance":1}]},"InstanceGroupType":"MASTER","InstanceType":"m4.large","Name":"Master Instance Group"}]' \
    --configurations '[{"Classification":"spark","Properties":{}}]' \
    --scale-down-behavior TERMINATE_AT_TASK_COMPLETION --region us-east-1 \
    --auto-terminate
