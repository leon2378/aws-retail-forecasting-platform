# AWS deployment and operations

This runbook targets **ap-southeast-2 (Sydney)**. The application works locally without AWS. On 8 October 2026, the 43-resource foundation was deployed and passed 21 live smoke checks, then all 43 resources were removed to stop ongoing project charges. The project is currently running locally. The commands below create billable resources only when the deployment steps are explicitly executed. Review a fresh saved plan and expected costs before redeploying.

The completed smoke checks verified resource configuration, HTTPS frontend delivery, Lambda health and release routes, same-origin API routing, the expected empty-catalog response and rejection of the public release-demo action. They did not exercise SageMaker training, processing, Batch Transform or forecast publication. No ML pipeline or model version was published during that foundation test. The Linux image build, offline ML rehearsal and inference HTTP protocol have since passed local validation. Live SageMaker execution, model registration and DynamoDB publication remain unverified; optional delivery, ClearML and scheduled ingestion remain disabled.

## Rehearse the ML workflow locally

Install the ML/AWS extras and import M5 as described in the [main README](../README.md). Then run the job rehearsal without AWS credentials:

```powershell
./.venv/Scripts/python.exe -m aws.local_validate --dataset data/dataset.json --cutoff 1913 --output build/local-rehearsal
```

The command runs the actual Prepare, Train and Evaluate functions for both candidates, checks the separation of historical training data and held-out labels, reloads each compressed model artifact and performs inference with training calls prohibited. It validates forecast dates, interval ordering, chronological backtests and inventory comparisons under three stock/lead-time scenarios. `build/local-rehearsal/report.json` records source provenance, dependency versions, measured gate outcomes and output paths. Generated artifacts contain private data and remain under the ignored `build/` directory.

Use a new or empty output directory; the command never removes existing artifacts. The default cutoff leaves the final 28 days for scoring. A rejected candidate is still inferred locally for comparison, whereas the AWS pipeline would stop before its Batch Transform and publication. This rehearsal does not execute the container in SageMaker, register model versions or publish results to DynamoDB. Validate one complete AWS replay separately before enabling a schedule.

Optionally reproduce the rehearsal inside the Linux image. Build it locally, mount the imported dataset read-only and write to a fresh ignored output folder. The rehearsal container has no network access and is limited to two CPUs:

```powershell
docker build --platform linux/amd64 -t shelfcast:local .
$linuxDatasetPath = (Resolve-Path data/dataset.json).Path
$linuxOutputPath = Join-Path (Get-Location).Path ('build/linux-rehearsal-' + (Get-Date -Format 'yyyyMMddHHmmssfff'))
New-Item -ItemType Directory -Path $linuxOutputPath | Out-Null
docker run --rm --network none --cpus 2 `
    --mount "type=bind,source=$linuxDatasetPath,target=/input/dataset.json,readonly" `
    --mount "type=bind,source=$linuxOutputPath,target=/output" `
    --entrypoint python shelfcast:local -m aws.local_validate `
    --dataset /input/dataset.json --cutoff 1913 --output /output/rehearsal
```

The report is saved under `$linuxOutputPath/rehearsal/report.json`. Image building can download the base image and dependencies; the rehearsal itself runs offline and uses no AWS resources.

Both completed private M5 rehearsals covered 36 series, 72 unique 28-day forecasts and 972 policy simulations each. The native Windows environment used NumPy 2.5.3 and XGBoost 3.4.1, yielding 109.02% XGBoost WAPE and 76.69% coverage. The Python 3.11 Linux image used NumPy 2.4.6 and XGBoost 3.2.0, yielding 110.21% WAPE and 76.36% coverage. Both gates rejected XGBoost against the same 108.07% seasonal WAPE and passed the seasonal candidate at parity with 82.34% coverage. Keep results attached to their recorded dependency versions; these runs do not establish an overall accuracy improvement or a successful AWS release.

The Linux image's `/ping` health route and `/invocations` JSON-lines inference route passed protocol checks for all 72 forecasts from each native and Linux artifact set. The served forecasts matched each artifact's expected output exactly and omitted held-out actuals. Invalid identities, malformed JSON, empty bodies and oversized requests returned HTTP 400. This validates the custom inference container locally, including loading native-trained artifacts; AWS Batch Transform itself remains untested.

The image's Prepare, Train and Evaluate CLI stages also passed a 36-series seasonal smoke test using SageMaker-style `/opt/ml` paths. This verifies those local entry points and does not exercise SageMaker orchestration or publication.

## Prerequisites

Use Python 3.11+, AWS CLI v2, Terraform 1.6+, and Docker with Linux containers for a local image build. CodeBuild can build the Linux image instead. Authenticate through a normal AWS profile or IAM Identity Center; do not put access keys in this repository, Terraform variables or images.

```powershell
$env:AWS_DEFAULT_REGION = 'ap-southeast-2'
aws sts get-caller-identity
aws configure list
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e '.[ml,aws]'
./.venv/Scripts/python.exe -m unittest discover -s tests -v
./.venv/Scripts/python.exe -m aws.package
terraform -chdir=infra init -backend=false
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
```

The default job type is `ml.m5.xlarge`. Check the three distinct SageMaker quotas in Sydney. A zero limit blocks that job even if your IAM permissions allow it:

```powershell
aws service-quotas get-service-quota --service-code sagemaker --quota-code L-CCE2AFA6 --region ap-southeast-2
aws service-quotas get-service-quota --service-code sagemaker --quota-code L-0307F515 --region ap-southeast-2
aws service-quotas get-service-quota --service-code sagemaker --quota-code L-7939E4EC --region ap-southeast-2
```

These codes identify `ml.m5.xlarge` **training**, **processing**, and **transform** usage respectively. Request at least 1 for each through Service Quotas, or choose a supported type with all three limits available. Quota approval does not reserve physical capacity. The CLI's `--instance-type` controls the first pipeline definition; `sagemaker_instance_type` controls definitions generated by CodeBuild. Keep those values aligned. Quota increases do not create jobs by themselves.

The ingestion Lambda uses the shared account concurrency pool by default. Its DynamoDB conditional lock and SageMaker request token coordinate overlapping invocations. Small accounts can have insufficient concurrency to reserve even one invocation: AWS requires 100 units to remain unreserved. Optionally add `reserved_concurrent_executions = 1` to the ingestion function only when the account has enough unreserved capacity, as explained in [Lambda's concurrency documentation](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html).

The deployment principal needs permission to manage the resources declared in `infra/`, create/pass the execution roles, and read AWS-managed CloudFront cache, origin-request and response-header policies. In particular, lacking `cloudfront:GetCachePolicy` can block even a Terraform plan. The application roles created by Terraform are narrowly scoped and are not a replacement for the deployment principal's permissions.

Terraform uses local state initially. Keep state and plans private; both can contain resource details. For shared or repeatable deployments, configure an encrypted, versioned S3 backend with state locking in an existing dedicated state bucket before applying. Do not commit state, tfvars, plans or credentials. The provider lock file should be committed.

## 1. Create the foundation

Run from the repository root. If a private configuration does not already exist, copy the example. Keep `schedule_enabled`, `enable_delivery` and `enable_clearml` disabled and `pipeline_definition_file` empty for the first foundation smoke test:

```powershell
if (-not (Test-Path infra/terraform.tfvars)) {
    Copy-Item infra/terraform.tfvars.example infra/terraform.tfvars
}
```

For the imported M5 evaluation data, set `initial_cutoff = 1913` in the private `infra/terraform.tfvars`: its 1,941 historical days then leave the final complete 28-day holdout for validation. Choose an earlier cutoff to replay several historical days. The example's default cutoff of 196 remains valid for the synthetic demo.

```powershell
terraform -chdir=infra plan -out=../build/foundation.tfplan
terraform -chdir=infra show ../build/foundation.tfplan
```

After approving the concrete plan, apply it:

```powershell
terraform -chdir=infra apply ../build/foundation.tfplan
terraform -chdir=infra output
```

This creates private, versioned S3 buckets, a DynamoDB table, an immutable ECR repository, roles, Lambda functions, an HTTP API, CloudFront delivery, logs, alarms, a model-package group and a **disabled** EventBridge rule. It does not create a persistent inference endpoint. The ML pipeline is added after its image exists. `/api/health` identifies AWS mode and `/api/releases` can return an empty history before a first batch. Forecast/catalog requests return HTTP 404 until a complete batch has been published. The frontend handles the empty catalog as **Awaiting the first forecast**, keeps planning controls disabled and offers a publication check; other failures remain visible as errors.

After a teardown, generate a new plan and use the new outputs. Do not reuse an old saved plan, former hosted URL or generated pipeline containing removed resource identifiers.

Export the non-secret resource identifiers for the following commands:

```powershell
$projectName = terraform -chdir=infra output -raw pipeline_name
$dataBucket = terraform -chdir=infra output -raw data_bucket
$frontendBucket = terraform -chdir=infra output -raw frontend_bucket
$ecrUri = terraform -chdir=infra output -raw ecr_repository_url
$sagemakerRole = terraform -chdir=infra output -raw sagemaker_role_arn
$resultsTable = terraform -chdir=infra output -raw results_table
$distributionId = terraform -chdir=infra output -raw distribution_id
$ingestFunction = terraform -chdir=infra output -raw ingest_function_name
$frontendUrl = terraform -chdir=infra output -raw frontend_url
```

## 2. Publish an image, pipeline and frontend

Use a new tag for every image because ECR tags are immutable:

```powershell
$ecrRegistry = $ecrUri.Split('/')[0]
$imageTag = 'release-' + (Get-Date -Format 'yyyyMMddHHmmss')
$imageUri = $ecrUri + ':' + $imageTag
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin $ecrRegistry
docker build --platform linux/amd64 -t $imageUri .
docker push $imageUri
./.venv/Scripts/python.exe -m aws.pipeline --name $projectName --role-arn $sagemakerRole --image-uri $imageUri --bucket $dataBucket --table-name $resultsTable --model-group $projectName --instance-type ml.m5.xlarge
```

Review ECR's image scan and `build/pipeline.json`. Set `pipeline_definition_file = "../build/pipeline.json"` in `infra/terraform.tfvars`. Save, review and apply the second plan:

```powershell
terraform -chdir=infra plan -out=../build/pipeline.tfplan
terraform -chdir=infra show ../build/pipeline.tfplan
terraform -chdir=infra apply ../build/pipeline.tfplan
aws s3 sync frontend/ "s3://$frontendBucket/" --cache-control max-age=300
aws cloudfront create-invalidation --distribution-id $distributionId --paths '/*'
```

The frontend uses same-origin `/api/*` requests. CloudFront routes those paths to API Gateway with caching disabled and forwards query strings and request bodies. Static files are served from private S3 through origin access control. A separate `API_BASE` is unnecessary for this deployment. The API is a public, throttled demo; add authentication before serving non-public information.

The frontend requests `/api/health` before the catalog so an empty AWS workspace is identified correctly. In AWS mode the model lab displays published release history, with an empty state when no decisions exist. Its release-demo action is disabled; the API also rejects that local-only action.

Terraform creates the first pipeline definition. Subsequent application releases update that definition through `aws sagemaker update-pipeline` or the deployment buildspec. The resource ignores definition drift to prevent a later infrastructure plan from restoring an old image. Terraform continues to manage its role, name and parallelism. For a later manual release, regenerate the definition against the new immutable image and run:

```powershell
aws sagemaker update-pipeline --pipeline-name $projectName --role-arn $sagemakerRole --pipeline-definition file://build/pipeline.json
```

## 3. Upload source data and replay one day

Download genuine M5 CSVs through the [official competition data page](https://www.kaggle.com/competitions/m5-forecasting-accuracy/data) and accept its terms yourself. The repository does not redistribute M5 files. Import a small documented selection first:

```powershell
./.venv/Scripts/python.exe -m retail_forecast import-m5 'C:/path/to/m5' --stores CA_1 TX_1 WI_1 --max-items 12
aws s3 cp data/dataset.json "s3://$dataBucket/raw/dataset.json"
```

For a synthetic infrastructure smoke test only, generate the same clearly labeled local demo archive:

```powershell
./.venv/Scripts/python.exe -c "from aws.jobs import write_json; from retail_forecast.data import demo_dataset; write_json('data/dataset.json', demo_dataset())"
aws s3 cp data/dataset.json "s3://$dataBucket/raw/dataset.json"
```

The uploaded archive contains historical future observations because it is the source of the replay simulation. Prepare splits it into observed history and a separate 28-day scoring-label channel. Train receives **only** observed history. The release gate evaluates earlier chronological backtests already inside the artifact; inference receives fitted artifacts and store/product requests. The final held-out labels reach only the publisher and inventory simulation, and are removed from public forecast responses.

Invoke ingestion, which owns the durable lock and idempotency token. Do not start the pipeline directly: manual executions without the replay lock are deliberately refused by the publisher.

```powershell
aws lambda invoke --function-name $ingestFunction --payload '{}' --cli-binary-format raw-in-base64-out build/ingest-response.json
Get-Content build/ingest-response.json
```

Take `execution_arn` from that response and inspect progress:

```powershell
$executionArn = (Get-Content build/ingest-response.json | ConvertFrom-Json).execution_arn
aws sagemaker describe-pipeline-execution --pipeline-execution-arn $executionArn
aws sagemaker list-pipeline-execution-steps --pipeline-execution-arn $executionArn
Invoke-RestMethod "$frontendUrl/api/health"
Invoke-RestMethod "$frontendUrl/api/catalog"
```

A passing run registers an Approved version, creates a batch model, forecasts the available models and publishes all selected series. Forecast rows are visible only after the completed marker, catalog and replay cursor commit together. A rejected candidate registers a Rejected version and audit entry, and keeps the cursor unchanged. The gate permits seasonal parity by default; an approval is not evidence of improved accuracy. XGBoost must meet measured WAPE and coverage thresholds. The local model lab's explicitly labeled rejection/approval demonstration is separate from AWS model promotion.

The first published cutoff establishes the minimum cloud replay date. The catalog points to the most recent fully published day. Forecasts for earlier published days remain selectable, and stored held-out sales allow API-side inventory simulation without retraining. The daily Lambda advances one historical day only after success. At the archive's last complete 28-day holdout it returns `replay_complete` without starting another job.

## 4. Enable scheduled ingestion

After verifying one full replay, set `schedule_enabled = true` and review/apply a new infrastructure plan. The default is `rate(1 day)`; this is a 24-hour interval, not a Sydney wall-clock appointment. Set the EventBridge expression deliberately if a fixed UTC execution time is wanted. Keep the archive fixed for a replay campaign. Replacing its series or start date needs a new environment/table to avoid mixing incompatible snapshots and catalog metadata.

If a job fails or a release is rejected, the next invocation detects its terminal execution and retries the **same** historical day with a new run ID. An in-progress execution causes a no-op. A lost StartPipelineExecution response reuses the recorded token and frozen S3 snapshot. A lost publication transaction response is resolved through the completed marker; retrying a committed run cannot rewrite its results. Partial forecast rows have no completion marker and cannot be served.

## Optional CodeBuild and CodePipeline delivery

Set `enable_delivery = true` and apply a reviewed infrastructure plan **after the first SageMaker pipeline exists**. Delivery stays wholly in AWS: upload a source archive to the versioned delivery bucket and start CodePipeline explicitly. GitHub remains the source repository; no webhook, bot or additional GitHub contributor is created.

```powershell
./.venv/Scripts/python.exe -m aws.package
$deliveryBucket = terraform -chdir=infra output -raw delivery_bucket
aws s3 cp build/source.zip "s3://$deliveryBucket/source/source.zip"
aws codepipeline start-pipeline-execution --name $projectName
```

CodeBuild installs ML/AWS dependencies, runs the behavior tests, builds/pushes a unique image, packages Lambda and generates a pipeline definition. A manual review stage stops deployment until approved. Review test output and ECR scan findings in the AWS console; the pipeline does not automatically reject vulnerabilities. The deployment stage updates both Lambda functions, updates the ML pipeline, copies static files and invalidates CloudFront. It performs application releases, not Terraform applies. Queued executions avoid concurrent application deploys.

## Optional ClearML on EC2

Terraform provides an optional private EC2 host, encrypted disk, SSM role and S3 artifact-prefix permissions. It requires a **reviewed AMI with ClearML Server already configured and running** plus SSM Agent. It does not install a server automatically. Follow [ClearML's official server deployment documentation](https://clear.ml/docs/latest/docs/deploying_clearml/clearml_server/) to build the AMI. Supply `clearml_ami_id`, `clearml_vpc_id`, `clearml_subnet_id`, then enable `enable_clearml` through a reviewed plan.

The subnet needs connectivity to SSM and S3 through suitable VPC endpoints or existing egress. Terraform intentionally creates no VPC, NAT gateway, public IP or inbound security-group rules. Endpoint security groups and S3 endpoint policies must permit the host's role. The host has HTTPS egress only; prepare the AMI before using it.

Use the Session Manager plugin and separate terminals to forward the server's normal ports (web 8080, API 8008, files 8081), for example:

```powershell
$clearmlInstance = terraform -chdir=infra output -raw clearml_instance_id
aws ssm start-session --target $clearmlInstance --document-name AWS-StartPortForwardingSession --parameters 'portNumber=8080,localPortNumber=8080'
```

Set `CLEARML_API_HOST`, `CLEARML_WEB_HOST`, `CLEARML_FILES_HOST` and ClearML API credentials in your local environment, then export completed evaluation reports:

```powershell
./.venv/Scripts/python.exe -m pip install -e '.[tracking]'
./.venv/Scripts/python.exe -m aws.clearml_export --evaluation-s3 "s3://$dataBucket/runs/<run-id>/evaluate/evaluation/evaluation.json" --artifacts-s3 "s3://$dataBucket/clearml/" --name '<run-id>'
```

This creates experiment comparisons and uploads report artifacts to S3. It is an explicit report-import workflow. SageMaker remains the execution orchestrator; its isolated training container does not need network access or ClearML credentials. Server access, backups, upgrades and AMI maintenance remain operational prerequisites. The optional EC2 host incurs continuous charges while running.

## Troubleshooting, costs and cleanup

Inspect the Lambda and `/aws/sagemaker/*` CloudWatch logs, execution step failures, ECR scan and release audit records. Lambda error alarms are defined, with no notification action attached. Add an SNS destination or another operator-owned alert target. There is no automated model drift monitoring yet.

For a replay stuck before an execution ARN is stored, first retry ingestion: its saved token is designed to recover a lost response. If snapshot loading or a validation error persists, fix the source or pipeline before retrying. Do not clear a lock while its SageMaker execution is active. A rejected release is an expected business gate outcome and its overall pipeline can still report Succeeded; inspect the registered model's approval state and release entry.

Billable components include each training/processing/transform job, S3 storage and object versions, DynamoDB requests/storage, CloudFront transfer, Lambda/API requests, logs, CodeBuild/CodePipeline when enabled, and optional EC2/EBS/networking. Processing jobs run for preparation, evaluation and publication/rejection; training has a two-hour limit and each processing job a one-hour limit. There is no always-on inference endpoint, but keeping daily schedules or ClearML enabled still incurs ongoing cost. Check [SageMaker pricing](https://aws.amazon.com/sagemaker-ai/pricing/) and the [AWS Pricing Calculator](https://calculator.aws/) for Sydney before deployment.

Job runtime caps are not spending limits. This configuration has no overall Batch Transform runtime cap and does not enforce a total cost ceiling. Monitor active jobs and stop unexpectedly long runs. Retained current S3 `runs/` objects, tagged ECR images and SageMaker job logs require deliberate cleanup or retention settings: the S3 rule expires only noncurrent run-object versions, the ECR rule expires only untagged images, and Terraform's 30-day log retention covers its Lambda/API log groups rather than SageMaker-created groups.

Disabling scheduled ingestion stops future scheduled runs, but retained storage, DynamoDB backups, logs and alarms can still incur charges. To stop the project's ongoing resource usage, archive wanted data locally, stop any active jobs, remove the deployed resources and verify that none remain. Billing can post already-incurred usage later; teardown cannot reverse earlier charges. Keep local development independent of the removed AWS environment until a new deployment is intended.

Disable schedules and wait for active executions before teardown. Preserve wanted model reports and source archives. S3 buckets use `force_destroy = false`, so Terraform will refuse to delete nonempty buckets; review object versions and retention before any explicit cleanup. Tagged ECR images and registered package/artifact records also need deliberate retention management. Use a reviewed destroy plan once those decisions are made. The application foundation has no production backup/restore drill or live load-test evidence yet.

References: [SageMaker pipeline schema](https://aws-sagemaker-mlops.github.io/sagemaker-model-building-pipeline-definition-JSON-schema/), [custom inference container requirements](https://docs.aws.amazon.com/sagemaker/latest/dg/your-algorithms-inference-code.html), [Model Registry deployment](https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry-deploy.html), and [transaction IAM permissions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html).
