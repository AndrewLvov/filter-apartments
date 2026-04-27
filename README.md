Filter apartments by specific criteria and send the results to a Telegram chat.

Deployment
Build new version of app:
```shell
docker build . -t filter-apartments
docker tag filter-apartments:latest 741769145503.dkr.ecr.eu-central-1.amazonaws.com/filter-apartments:0.1.9
```

Here the version is 0.1.9, incremented from the previous version 0.1.8 in my example.
Push the new version to AWS ECR:
```shell
aws sso login  # if not logged in
aws ecr get-login-password --region eu-central-1 \
| docker login --username AWS --password-stdin 741769145503.dkr.ecr.eu-central-1.amazonaws.com

docker push 741769145503.dkr.ecr.eu-central-1.amazonaws.com/filter-apartments:0.1.9
```

Update version in values.yaml:
```yaml
image:
  repository: 741769145503.dkr.ecr.eu-central-1.amazonaws.com/filter-apartments
  IfNotPresenIfNotPresent: IfNotPresent
  tag: "0.1.9"  # Update this line
```

Then upgrade the Helm release:
```shell
cd deplyoyment/helm/filter-apartments
helm upgrade filter-apartments .
```

New version might not be picked up immediately, you can force a restart of the pod:
```shell
k delete pod filter-apartments-filterapartments-5477c4b9fc-68nmb
# Replace with your actual pod name
```