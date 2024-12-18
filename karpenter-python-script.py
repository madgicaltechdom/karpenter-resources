import boto3
import subprocess
import os
import json
import yaml
import sys

def run_command(command):
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise Exception(f"Command failed with exit code {e.returncode}: {e.stderr.strip()}") from e

# Set initial variables
KARPENTER_NAMESPACE = os.environ.get("KARPENTER_NAMESPACE")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")

# Set other variables from your cluster configuration
AWS_PARTITION = "aws"
AWS_REGION = os.environ.get("AWS_REGION")
OIDC_ENDPOINT = run_command(f"aws eks describe-cluster --name {CLUSTER_NAME} --query 'cluster.identity.oidc.issuer' --output text")
AWS_ACCOUNT_ID = run_command("aws sts get-caller-identity --query 'Account' --output text")
K8S_VERSION = os.environ.get("K8S_VERSION")
AMD_AMI_ID = os.environ.get("AMD_AMI_ID")
KARPENTER_ROLE_CLOUDFORMATION_TEMPLATE_PATH = "./karpenter_role_cloudformation.yaml"

# Step 1: Deploy the CloudFormation stack for Karpenter IAM roles
try:
    print("Deploying CloudFormation stack for Karpenter IAM roles...")
    run_command(
        f"aws cloudformation deploy "
        f"--stack-name 'Karpenter-{CLUSTER_NAME}' "
        f"--template-file '{KARPENTER_ROLE_CLOUDFORMATION_TEMPLATE_PATH}' "
        f"--capabilities CAPABILITY_NAMED_IAM "
        f"--parameter-overrides ClusterName={CLUSTER_NAME}"
    )
    print("CloudFormation stack deployed successfully.")
except Exception as e:
    print(f"Failed to deploy CloudFormation stack: {e}")
    sys.exit(1)  # Terminate the script if this step fails

# Step 2: Ensure EC2 Spot service-linked role is created
try:
    print("Ensuring EC2 Spot service-linked role exists...")
    run_command("aws iam create-service-linked-role --aws-service-name spot.amazonaws.com || true")
    print("EC2 Spot service-linked role check completed.")
except Exception as e:
    print(f"Failed to create EC2 Spot service-linked role: {e}")
    sys.exit(1)  # Terminate the script if this step fails

# Step 3: Create IAM role for Karpenter controller and add
controller_trust_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Federated": f"arn:{AWS_PARTITION}:iam::{AWS_ACCOUNT_ID}:oidc-provider/{OIDC_ENDPOINT.split('//')[1]}"
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    f"{OIDC_ENDPOINT.split('//')[1]}:aud": "sts.amazonaws.com",
                    f"{OIDC_ENDPOINT.split('//')[1]}:sub": f"system:serviceaccount:{KARPENTER_NAMESPACE}:karpenter"
                }
            }
        }
    ]
}

iam_client = boto3.client('iam')
controller_role_name = f"KarpenterControllerRole-{CLUSTER_NAME}"
try:
    iam_client.create_role(
        RoleName=controller_role_name,
        AssumeRolePolicyDocument=json.dumps(controller_trust_policy)
    )
except iam_client.exceptions.EntityAlreadyExistsException:
    print(f"Role {controller_role_name} already exists")

try:
    iam_client.attach_role_policy(
        RoleName=controller_role_name,
        PolicyArn=f"arn:{AWS_PARTITION}:iam::{AWS_ACCOUNT_ID}:policy/KarpenterControllerPolicy-{CLUSTER_NAME}"
    )
    print(f"Policy KarpenterControllerPolicy-{CLUSTER_NAME} attached to {controller_role_name}.")
except Exception as e:
    print(f"Error attaching policy to role: {str(e)}")
# Step 4 and 5 are removed

# Step 6a: Select NG, get subnetes automatically and add tags to subnetes 
nodegroups = run_command(f"aws eks list-nodegroups --cluster-name {CLUSTER_NAME} --query 'nodegroups' --output text").split()

for nodegroup in nodegroups:
    subnets = run_command(f"aws eks describe-nodegroup --cluster-name {CLUSTER_NAME} --nodegroup-name {nodegroup} --query 'nodegroup.subnets' --output text").split()
    for subnet in subnets:
        run_command(f"aws ec2 create-tags --tags 'Key=karpenter.sh/discovery,Value={CLUSTER_NAME}' --resources {subnet}")

# Step 6b: Define your subnets manually (list of subnet IDs)
#subnets = [
#    "subnet-0632df4f63bd04b34",
#    "subnet-01abf3394bd1d80a8",
#    "subnet-09344001e5499b75e"
#]

# Loop through each subnet and add the necessary tags
for subnet in subnets:
    run_command(f"aws ec2 create-tags --tags 'Key=karpenter.sh/discovery,Value={CLUSTER_NAME}' --resources {subnet}")

# Step 7: Tag security groups

# Get the node group
nodegroup = run_command(
    f"aws eks list-nodegroups --cluster-name {CLUSTER_NAME} --query 'nodegroups[0]' --output text"
)
print(f"Nodegroup: {nodegroup}")

# Add label to the node group for node affinity
label_key = os.environ.get("NODEGROUP_LABEL")
label_value = nodegroup

# Create the labels dictionary
labels_dict = {
    "addOrUpdateLabels": {
        label_key: label_value
    }
}

# Convert the dictionary to JSON format
labels_json = json.dumps(labels_dict)

# Update the node group configuration to add or update the label
try:
    update_command = (
        f"aws eks update-nodegroup-config --cluster-name {CLUSTER_NAME} --nodegroup-name {nodegroup} "
        f"--labels '{labels_json}' --region {AWS_REGION}"
    )
    run_command(update_command)
    print(f"Label '{label_key}={label_value}' added to the node group '{nodegroup}'.")
except Exception as e:
    print(f"Failed to add label to the node group: {str(e)}")

# Get the launch template ID and version
launch_template = run_command(
    f"aws eks describe-nodegroup --cluster-name {CLUSTER_NAME} --nodegroup-name {nodegroup} "
    f"--query 'nodegroup.launchTemplate.{{id:id,version:version}}' --output text"
).replace("\t", ",")

launch_template_id, launch_template_version = launch_template.split(",")
print(f"Launch Template ID: {launch_template_id}")
print(f"Launch Template Version: {launch_template_version}")

# Get the security groups directly from LaunchTemplateData
security_groups = run_command(
    f"aws ec2 describe-launch-template-versions --launch-template-id {launch_template_id} "
    f"--versions {launch_template_version} --query 'LaunchTemplateVersions[0].LaunchTemplateData.SecurityGroupIds' --output json"
)

security_groups = json.loads(security_groups)
print(f"Security Groups: {security_groups}")

# Check if security groups were found
if not security_groups:
    raise Exception("No security groups found. Please check your launch template configuration.")

# Tag the security groups
run_command(
    f"aws ec2 create-tags --tags Key=karpenter.sh/discovery,Value={CLUSTER_NAME} --resources {' '.join(security_groups)}"
)

print("Security groups have been successfully tagged.")

# Step 8: Update aws-auth ConfigMap
# Define the new aws-auth entry
new_entry = {
    "groups": [
        "system:bootstrappers",
        "system:nodes",
        # If you intend to run Windows workloads, the kube-proxy group should be specified.
        # For more information, see https://github.com/aws/karpenter/issues/5099.
        # "eks:kube-proxy-windows"
    ],
    "rolearn": f"arn:{AWS_PARTITION}:iam::{AWS_ACCOUNT_ID}:role/KarpenterNodeRole-{CLUSTER_NAME}",
    "username": "system:node:{{EC2PrivateDNSName}}"
}

# Fetch existing aws-auth ConfigMap
run_command(f"kubectl get configmap aws-auth -n {KARPENTER_NAMESPACE} -o yaml > /tmp/aws-auth.yaml")

# Read and update the aws-auth ConfigMap
with open("/tmp/aws-auth.yaml", "r") as f:
    config = yaml.safe_load(f)

# Check if the new entry is already in mapRoles
updated = False
if "data" in config and "mapRoles" in config["data"]:
    map_roles = yaml.safe_load(config["data"]["mapRoles"])
    
    # Check if the new role already exists
    for role in map_roles:
        if role["rolearn"] == new_entry["rolearn"]:
            print(f"Role {new_entry['rolearn']} already updated.")
            updated = True
            break

    # If not updated, append the new entry
    if not updated:
        map_roles.append(new_entry)
        config["data"]["mapRoles"] = yaml.dump(map_roles, default_flow_style=False)
        print("New entry added to aws-auth ConfigMap.")

else:
    raise Exception("mapRoles section not found in aws-auth ConfigMap.")

# If it was updated, apply the changes
if not updated:
    # Write the updated ConfigMap to a temporary file
    with open("/tmp/aws-auth-updated.yaml", "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    # Apply the updated ConfigMap
    run_command(f"kubectl apply -f /tmp/aws-auth-updated.yaml")

    print("aws-auth ConfigMap updated successfully.")

# Step 9: Install the Karpenter through local Helm chart

KARPENTER_VERSION = os.environ.get("KARPENTER_VERSION")
KARPENTER_HELM_DIR = "karpenter"

# Update command to refer to the local Helm chart
command = (
    f"helm template karpenter {KARPENTER_HELM_DIR} --namespace \"{KARPENTER_NAMESPACE}\" "
    f"--set \"settings.clusterName={CLUSTER_NAME}\" "
    f"--set settings.isolatedVPC=true "
    f"--set \"serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn=arn:{AWS_PARTITION}:iam::{AWS_ACCOUNT_ID}:role/KarpenterControllerRole-{CLUSTER_NAME}\" "
    f"--set \"settings.interruptionQueue={CLUSTER_NAME}\" "
    f"--set controller.resources.requests.cpu=1 "
    f"--set controller.resources.requests.memory=1Gi "
    f"--set controller.resources.limits.cpu=1 "
    f"--set controller.resources.limits.memory=1Gi "
    f"--wait > karpenter.yaml"
)

# Run the command to generate karpenter.yaml
def run_command(cmd):
    result = os.system(cmd)
    if result != 0:
        raise Exception(f"Command failed: {cmd}")

run_command(command)
print("karpenter.yaml created successfully.")

# Step 10: Modify karpenter.yaml for node affinity 
def modify_karpenter_yaml(file_path, nodegroup):
    # Load all YAML documents from the file
    with open(file_path, 'r') as file:
        documents = list(yaml.safe_load_all(file))

    # Define new affinity rules
    affinity_rules = {
        'affinity': {
            'nodeAffinity': {
                'requiredDuringSchedulingIgnoredDuringExecution': {
                    'nodeSelectorTerms': [
                        {
                            'matchExpressions': [
                                {
                                    'key': 'karpenter.sh/nodepool',
                                    'operator': 'DoesNotExist'
                                },
                                {
                                    'key': 'karpenter/nodegroup',
                                    'operator': 'In',
                                    'values': [nodegroup]
                                }
                            ]
                        }
                    ]
                }
            },
            'podAntiAffinity': {
                'requiredDuringSchedulingIgnoredDuringExecution': [
                    {
                        'topologyKey': 'kubernetes.io/hostname',
                        'labelSelector': {
                            'matchLabels': {
                                'app.kubernetes.io/instance': 'karpenter',
                                'app.kubernetes.io/name': 'karpenter'
                            }
                        }
                    }
                ]
            }
        }
    }

    # Find and update the relevant document (e.g., Deployment)
    for doc in documents:
        if isinstance(doc, dict) and doc.get('kind') == 'Deployment':
            if 'spec' in doc and 'template' in doc['spec']:
                if 'affinity' in doc['spec']['template']['spec']:
                    doc['spec']['template']['spec']['affinity'].update(affinity_rules['affinity'])
                else:
                    doc['spec']['template']['spec'].update(affinity_rules)
            break

    # Write all documents back to the file
    with open(file_path, 'w') as file:
        yaml.safe_dump_all(documents, file, default_flow_style=False)

    print(f"Modified {file_path} with node affinity rules.")

# Update Karpenter Image in karpenter.yaml
def update_karpenter_image(file_path, new_image):
    # Load all YAML documents from the file
    with open(file_path, 'r') as file:
        documents = list(yaml.safe_load_all(file))

    # Find and update the relevant document (e.g., Deployment)
    for doc in documents:
        if isinstance(doc, dict) and doc.get('kind') == 'Deployment':
            if 'spec' in doc and 'template' in doc['spec']:
                containers = doc['spec']['template']['spec'].get('containers', [])
                for container in containers:
                    if container.get('name') == 'controller':
                        old_image = container.get('image')
                        print(f"Old image: {old_image}")
                        container['image'] = new_image
                        print(f"Updated image to {new_image} in {file_path}")
                        break

    # Write all documents back to the file
    with open(file_path, 'w') as file:
        yaml.safe_dump_all(documents, file, default_flow_style=False)

    print(f"Updated {file_path} with new image: {new_image}")

# Example usage: modify the karpenter.yaml file
kube_config_path = "karpenter.yaml"
karpenter_image = os.environ.get("KARPENTER_IMAGE")

modify_karpenter_yaml(kube_config_path, nodegroup)
update_karpenter_image(kube_config_path, karpenter_image)


# Step 11: Create namespace and NodePool CRD, and apply karpenter.yaml through local karpenter files
KARPENTER_CRD_DIR = "karpenter/crds" 

# Apply CRDs from the local directory

# Apply CRD for karpenter.sh_nodepools.yaml
try:
    run_command(f"kubectl apply -f \"{KARPENTER_CRD_DIR}/karpenter.sh_nodepools.yaml\"")
    print("CRD karpenter.sh_nodepools.yaml applied successfully.")
except Exception as e:
    print(f"Error applying CRD karpenter.sh_nodepools.yaml: {str(e)}")

# Apply CRD for karpenter.k8s.aws_ec2nodeclasses.yaml
try:
    run_command(f"kubectl apply -f \"{KARPENTER_CRD_DIR}/karpenter.k8s.aws_ec2nodeclasses.yaml\"")
    print("CRD karpenter.k8s.aws_ec2nodeclasses.yaml applied successfully.")
except Exception as e:
    print(f"Error applying CRD karpenter.k8s.aws_ec2nodeclasses.yaml: {str(e)}")

# Apply CRD for karpenter.sh_nodeclaims.yaml
try:
    run_command(f"kubectl apply -f \"{KARPENTER_CRD_DIR}/karpenter.sh_nodeclaims.yaml\"")
    print("CRD karpenter.sh_nodeclaims.yaml applied successfully.")
except Exception as e:
    print(f"Error applying CRD karpenter.sh_nodeclaims.yaml: {str(e)}")

# Apply the Karpenter configuration
run_command("cat karpenter.yaml")
run_command("kubectl apply -f karpenter.yaml")
print("Karpenter deployed")

# Step 12: Create default NodePools
nodepool_yaml = f"""
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: on-demand
spec:
  template:
    spec:
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: kubernetes.io/os
          operator: In
          values: ["linux"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["on-demand"]
        - key: capacity-spread
          operator: In
          values:
          - "1"
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]
        - key: karpenter.k8s.aws/instance-size
          operator: In
          values: ["xlarge","2xlarge","4xlarge","8xlarge"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      expireAfter: 720h # 30 * 24h = 720h
  limits:
    cpu: 1000
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: spot
spec:
  template:
    spec:
      requirements:
        - key: kubernetes.io/arch
          operator: In
          values: ["amd64"]
        - key: kubernetes.io/os
          operator: In
          values: ["linux"]
        - key: karpenter.sh/capacity-type
          operator: In
          values: ["spot"]
        - key: capacity-spread
          operator: In
          values:
          - "2"
        - key: karpenter.k8s.aws/instance-category
          operator: In
          values: ["c", "m", "r"]
        - key: karpenter.k8s.aws/instance-generation
          operator: Gt
          values: ["4"]
        - key: karpenter.k8s.aws/instance-size
          operator: In
          values: ["xlarge","2xlarge","4xlarge","8xlarge"]
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: default
      expireAfter: 720h # 30 * 24h = 720h
  limits:
    cpu: 1000
  disruption:
    consolidationPolicy: WhenEmptyOrUnderutilized
    consolidateAfter: 1m
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: default
spec:
  amiFamily: AL2023 # Amazon Linux 2023
  role: "KarpenterNodeRole-{CLUSTER_NAME}" # replace with your cluster name
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery: "{CLUSTER_NAME}" # replace with your cluster name
  securityGroupSelectorTerms:
    - tags:
        karpenter.sh/discovery: "{CLUSTER_NAME}" # replace with your cluster name
  amiSelectorTerms:
    - id: "{AMD_AMI_ID}"
"""

# Apply the NodePool YAML
with open("nodepool.yaml", "w") as f:
    f.write(nodepool_yaml)

run_command("kubectl apply -f nodepool.yaml")

print("Karpenter is now active and ready to begin provisioning nodes.")
