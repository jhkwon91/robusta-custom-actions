from robusta.api import (
    ActionParams,
    PodEvent,
    action,
    MarkdownBlock,
)
from hikaru.model.rel_1_26.v1 import Deployment
from kubernetes import client


class ScaleDownParams(ActionParams):
    dry_run: bool = False
    notify_channel: str = ""


@action
def scale_down_on_crash(event: PodEvent, params: ScaleDownParams):
    """
    CrashLoopBackOff 상태의 Pod가 속한 Deployment를 찾아
    복제본을 0으로 줄입니다.
    dry_run=True로 설정하면 실제 변경 없이 알림만 발송합니다.
    """
    pod = event.get_pod()
    if not pod:
        return

    pod_name = pod.metadata.name
    namespace = pod.metadata.namespace
    labels = pod.metadata.labels or {}

    # 1. Pod가 속한 Deployment 찾기
    deployment = _find_deployment(namespace, labels)
    if not deployment:
        event.add_enrichment([
            MarkdownBlock(
                f"⚠️ *스케일 다운 실패*\n"
                f"Pod `{pod_name}` 의 상위 Deployment를 찾을 수 없습니다.\n"
                f"직접 배포된 Pod이거나 다른 컨트롤러가 관리 중입니다."
            )
        ])
        return

    deployment_name = deployment.metadata.name
    current_replicas = deployment.spec.replicas or 0

    # 2. 이미 0이면 작업 불필요
    if current_replicas == 0:
        event.add_enrichment([
            MarkdownBlock(
                f"ℹ️ *스케일 다운 불필요*\n"
                f"Deployment `{deployment_name}` 의 복제본이 이미 0입니다."
            )
        ])
        return

    # 3. dry_run 모드: 실제 변경 없이 알림만 발송
    if params.dry_run:
        event.add_enrichment([
            MarkdownBlock(
                f"🔍 *[DRY RUN] 스케일 다운 시뮬레이션*\n"
                f"Deployment: `{deployment_name}`\n"
                f"Namespace: `{namespace}`\n"
                f"현재 복제본: `{current_replicas}` → 변경 예정: `0`\n"
                f"dry_run=False 설정 시 실제 적용됩니다."
            )
        ])
        return

    # 4. 실제 스케일 다운 실행
    apps_v1 = client.AppsV1Api()
    apps_v1.patch_namespaced_deployment(
        name=deployment_name,
        namespace=namespace,
        body={"spec": {"replicas": 0}},
    )

    event.add_enrichment([
        MarkdownBlock(
            f"🛑 *자동 스케일 다운 실행됨*\n"
            f"Deployment: `{deployment_name}`\n"
            f"Namespace: `{namespace}`\n"
            f"복제본: `{current_replicas}` → `0`\n\n"
            f"Pod `{pod_name}` 의 반복 재시작으로 인해 자동 조치했습니다.\n"
            f"복구하려면 아래 명령을 실행하세요.\n"
            f"```\n"
            f"kubectl scale deployment {deployment_name} "
            f"--replicas=1 -n {namespace}\n"
            f"```"
        )
    ])


def _find_deployment(namespace: str, pod_labels: dict):
    """Pod 레이블과 일치하는 Deployment를 찾습니다."""
    apps_v1 = client.AppsV1Api()
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace)

    for deployment in deployments.items:
        selector = deployment.spec.selector.match_labels or {}
        # Deployment의 셀렉터 레이블이 Pod 레이블에 모두 포함되면 매칭
        if all(pod_labels.get(k) == v for k, v in selector.items()):
            return deployment

    return None
