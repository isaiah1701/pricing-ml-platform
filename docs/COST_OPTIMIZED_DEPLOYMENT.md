# Cost-optimized learning deployment

The active launch uses `cost_optimized=true`: one on-demand `r5a.large` shared
worker (2 vCPUs, 16 GiB RAM) in one Availability Zone. Serving, MLflow, Airflow,
GitOps controllers, and monitoring share this node. Prefix delegation supports
the controller pod count. Airflow runs one task at a time and native libraries
use one thread to leave capacity for serving and cluster services.
Airflow parses one DAG file at a time, allows 180 seconds for cold imports, and
reparses files every five minutes. Component probes query recent database
heartbeats without repeatedly importing the full Airflow CLI. MLflow has a
cold-start probe allowance and does not overlap old/new instances during updates.

The budget values use single replicas, fixed worker/serving capacity, two-day
Prometheus retention, smaller persistent volumes, two single-AZ `db.t4g.micro`
PostgreSQL instances, and a single `cache.t4g.micro` Redis node. One NAT gateway
replaces the per-AZ gateways and paid interface endpoints are disabled; private
workloads use NAT and the free S3 gateway endpoint. Encryption, RDS backups,
secret handling, HTTPS, and private worker subnets remain enabled.

This is not highly available: a worker/AZ outage interrupts the entire stack.
EKS control-plane, NAT, NLB, storage, and database charges remain in addition
to worker compute. This profile reduces cost within the existing EKS design;
it does not turn EKS into a free-tier service. Keep datasets and backups before
tearing down a learning session.

At AWS public on-demand London rates checked through the AWS Pricing API on
2026-10-15, 730 hours/month is approximately $287 in baseline resources, or
roughly $300/month before tax and variable usage. This is not a billing cap:

| Resource | Approximate USD/month |
| --- | ---: |
| One r5a.large worker | 97.09 |
| Standard-support EKS control plane | 73.00 |
| One NAT gateway (hourly only) | 36.50 |
| Two db.t4g.micro PostgreSQL instances | 26.28 |
| One cache.t4g.micro Redis node | 13.14 |
| Network Load Balancer (hourly only) | 19.32 |
| 53 GiB gp3 worker/PVC storage | 4.92 |
| 40 GiB RDS gp3 storage | 5.32 |
| Three public IPv4 addresses | 10.95 |
| One Route 53 hosted zone | 0.50 |

NAT data processing, NLB capacity units, transfer, DNS queries, S3/ECR storage,
CloudWatch logs, KMS, Secrets Manager, backups beyond allowances, and any CPU
surplus credits are additional. Credits, discounts, and actual month length
change the bill. Rates and active resources should be rechecked before reuse.

Setting `cost_optimized=false` restores the dedicated Terraform topology; Helm
replicas, scheduling, and resources must also be raised and aligned before
returning to a high-availability deployment.
