# Terraform

Scaffold only. The R1 milestone runs everything from `infra/docker-compose.yml`;
these files pin the shape of the R2 cloud deployment (ECR + Fargate + CloudWatch)
so the migration is a fill-in rather than a redesign.

```bash
terraform init
terraform validate
```
