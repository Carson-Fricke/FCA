#!/usr/bin/env bash
# Verify actual rollout readiness, rather than the desired app template alone.
set -euo pipefail
app_name="$1"
resource_group="$2"
expected_image="$3"
required_health="$4"
deadline=$((SECONDS + 300))
while (( SECONDS < deadline )); do
  app="$(az containerapp show --name "$app_name" --resource-group "$resource_group" --output json)"
  latest="$(jq -r '.properties.latestRevisionName // empty' <<<"$app")"
  ready="$(jq -r '.properties.latestReadyRevisionName // empty' <<<"$app")"
  if [[ -n "$latest" ]]; then
    revision="$(az containerapp revision show --name "$app_name" --resource-group "$resource_group" --revision "$latest" --output json)"
    if ! jq -e --arg image "$expected_image" '.properties.template.containers | length > 0 and all(.[]; .image == $image)' <<<"$revision" >/dev/null; then
      echo "::error::$app_name latest revision does not run the published image"
      exit 1
    fi
    state="$(jq -r '.properties.provisioningState // empty' <<<"$revision")"
    if [[ "$state" == "Failed" ]]; then
      echo "::error::$app_name latest revision failed provisioning"
      exit 1
    fi
    replicas="$(az containerapp replica list --name "$app_name" --resource-group "$resource_group" --revision "$latest" --output json)"
    if [[ "$latest" == "$ready" ]] &&
       jq -e 'length > 0 and all(.[]; .properties.containers | length > 0 and all(.[]; .ready == true and .runningState == "Running"))' <<<"$replicas" >/dev/null &&
       { [[ "$required_health" == "ready" ]] || jq -e '.properties.healthState == "Healthy"' <<<"$revision" >/dev/null; }; then
      echo "$app_name latest revision $latest is ready on $expected_image"
      exit 0
    fi
  fi
  sleep 5
done
echo "::error::$app_name did not make its latest published revision ready within 300 seconds"
exit 1
