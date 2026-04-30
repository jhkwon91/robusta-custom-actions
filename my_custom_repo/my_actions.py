from robusta.api import (
    ActionParams,
    PodEvent,
    action,
    MarkdownBlock,
)
from kubernetes import client
import requests


class ScaleDownParams(ActionParams):
    dry_run: bool = False
    cmdb_url: str = "http://mock-cmdb.default.svc.cluster.local/service"
    cmdb_token: str = ""
    cmdb_timeout: int = 5


@action
def scale_down_on_crash(event: PodEvent, params: ScaleDownParams):
    """
    CrashLoopBackOff 상태의 Pod가 속한 Deployment를 찾아
    CMDB에서 서비스 중요도를 조회한 뒤 분기합니다.
    - P1/P2: 자동 스케일 다운 보류, 담당자 알림만 발송
    - P3 이하: 복제본을 0으로 줄이고 담당자 정보 함께 알림
    """
    pod = event.get_pod()
    if not pod:
        return

    pod_name = pod.metadata.name
    namespace = pod.metadata.namespace
    labels = pod.metadata.labels or {}

    # 1. 서비스명 추출 — 레이블 app 값이 가장 안정적
    service_name = (
        labels.get("app")
        or labels.get("app.kubernetes.io/name")
        or pod_name.rsplit("-", 2)[0]
    )

    # 2. CMDB 조회
    cmdb_data = _query_cmdb(
        url=params.cmdb_url,
        service_name=service_name,
        token=params.cmdb_token,
        timeout=params.cmdb_timeout,
    )

    # CMDB 조회 실패 시 기본값으로 P3 처리
    tier = cmdb_data.get("tier", "P3") if cmdb_data else "P3"
    owner = cmdb_data.get("owner", "미확인") if cmdb_data else "미확인"
    slack = cmdb_data.get("slack", "@oncall") if cmdb_data else "@oncall"
    team = cmdb_data.get("team", "미확인") if cmdb_data else "미확인"

    tier_emoji = {"P1": "🔴", "P2": "🟠", "P3": "🟡"}.get(tier, "⚪")

    # 3. P1/P2 — 자동 조치 보류, 담당자에게 알림만
    if tier in ("P1", "P2"):
        event.add_enrichment([
            MarkdownBlock(
                f"{tier_emoji} *{tier} 서비스 — 자동 스케일 다운 보류*\n"
                f"서비스: `{service_name}`\n"
                f"담당자가 직접 판단 후 조치해야 합니다.\n\n"
                f"담당자: {owner}\n"
                f"팀: {team}\n"
                f"Slack: {slack}\n\n"
                f"수동 스케일 다운이 필요하면 아래 명령을 실행하세요.\n"
                f"```\n"
                f"kubectl scale deployment {service_name} "
                f"--replicas=0 -n {namespace}\n"
                f"```"
            )
        ])
        return

    # 4. P3 이하 — Deployment 찾아서 스케일 다운
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

    if current_replicas == 0:
        event.add_enrichment([
            MarkdownBlock(
                f"ℹ️ *스케일 다운 불필요*\n"
                f"Deployment `{deployment_name}` 의 복제본이 이미 0입니다."
            )
        ])
        return

    if params.dry_run:
        event.add_enrichment([
            MarkdownBlock(
                f"🔍 *[DRY RUN] 스케일 다운 시뮬레이션*\n"
                f"Deployment: `{deployment_name}`\n"
                f"Namespace: `{namespace}`\n"
                f"서비스 중요도: `{tier}` — 자동 조치 대상\n"
                f"현재 복제본: `{current_replicas}` → 변경 예정: `0`\n"
                f"dry_run=False 설정 시 실제 적용됩니다."
            )
        ])
        return

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
            f"서비스 중요도: `{tier}`\n"
            f"복제본: `{current_replicas}` → `0`\n\n"
            f"담당자: {owner} ({slack})\n\n"
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
        if all(pod_labels.get(k) == v for k, v in selector.items()):
            return deployment

    return None


def _query_cmdb(url: str, service_name: str, token: str, timeout: int) -> dict:
    """CMDB API를 호출하고 결과를 반환합니다. 실패 시 None을 반환합니다."""
    try:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        response = requests.get(
            url,
            params={"service": service_name},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.Timeout:
        print(f"[scale_down_on_crash] CMDB 응답 시간 초과: {url}")
        return None
    except requests.exceptions.ConnectionError:
        print(f"[scale_down_on_crash] CMDB 연결 실패: {url}")
        return None
    except Exception as e:
        print(f"[scale_down_on_crash] CMDB 조회 오류: {e}")
        return None
