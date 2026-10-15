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

Setting `cost_optimized=false` restores the dedicated Terraform topology; Helm
replicas, scheduling, and resources must also be raised and aligned before
returning to a high-availability deployment.
