"""Generate the polished MLOps platform completion checklist PDF."""

from __future__ import annotations

import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "docs" / "MLOps_Completion_Roadmap.pdf"

SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "01",
        "Data Validation & Training DAG",
        (
            "Add training/validation.py: validate required columns, nulls, types, category values, coordinate bounds, non-negative counts, and target range; return a machine-readable report.",
            "Update pipelines/feature-build.py so validation runs after transformation and fails the DAG before Feast materialization when any blocking rule fails.",
            "Add pipelines/training.py with tasks for validation → point-in-time Feast retrieval → train → evaluate → log to MLflow → conditional registration.",
            "Reuse training/train.py inside the DAG; log dataset SHA-256, Git SHA, feature-service name, parameters, MAE/RMSE/R², model, and evaluation report.",
            "Add a simple baseline run and the CatBoost candidate to the same MLflow experiment so the UI provides a direct comparison.",
            "Define the registration gate in code: candidate must beat the configured MAE threshold and complete artifact-reload verification before registration.",
            "Add tests/test_validation.py and tests/test_training_dag.py, including one valid dataset and one dataset that is deliberately blocked.",
        ),
    ),
    (
        "02",
        "Serving Observability & Model Refresh",
        (
            "Add prometheus-client and expose GET /metrics from serving/app/api.py.",
            "Instrument request totals by endpoint/status, prediction latency histogram, prediction failures, readiness, loaded model version, and predicted-price histogram.",
            "Extend model_loader.py to resolve and retain the MLflow model version behind the configured alias, not only the model object.",
            "Add a safe refresh mechanism: periodically check the alias, atomically swap models after a successful load, and retain the current model if refresh fails.",
            "Extend tests/test_serving.py and tests/test_model_loader.py to verify metrics, version reporting, successful refresh, and failed-refresh fallback.",
            "Run a reproducible load test and record request rate plus p50/p95/p99 latency in docs/SERVING.md.",
        ),
    ),
    (
        "03",
        "Drift Monitoring, Dashboards & Alerts",
        (
            "Create monitoring/drift_check.py using Evidently; compare a configurable production window with the training reference dataset.",
            "Check numerical and categorical feature drift plus prediction drift; write JSON and HTML reports with timestamps and model-version metadata.",
            "Create a deliberately shifted test dataset and tests/test_drift_check.py; verify the normal sample passes and shifted features are flagged.",
            "Expose per-feature drift score, dataset drift state, drifted-feature count, and last-success timestamp to Prometheus.",
            "Add a scheduled Airflow monitoring DAG that runs the drift check and emits a retraining event only after the configured threshold is exceeded.",
            "Add three version-controlled Grafana JSON dashboards: infrastructure/service health, model/drift health, and Airbnb pricing business metrics.",
            "Add Prometheus rules for stale monitoring, feature drift, prediction drift, elevated error rate, and p95 latency.",
            "Add docs/runbooks/ entries describing impact, diagnosis, mitigation, rollback, and ownership for every alert.",
        ),
    ),
    (
        "04",
        "Retraining, Approval, Promotion & Rollback",
        (
            "Create retraining/retrain.py as an Airflow DAG triggered by the monitoring event, with the feature/data window recorded as run metadata.",
            "Evaluate challenger and current champion on the identical held-out dataset; require a configured minimum MAE improvement and non-regression guardrails for RMSE and key segments.",
            "If gates fail, retain the champion and record the rejection reason; if gates pass, register the challenger without moving the production alias.",
            "Add a human approval task before production promotion using an Airflow-supported approval mechanism and an audit trail.",
            "On approval, save the previous version as the rollback target and atomically move the MLflow production/champion alias.",
            "Run post-promotion smoke and performance checks; automatically restore the previous alias when health, latency, or model checks fail.",
            "Add tests for rejected challenger, approved promotion, missing approval, failed deployment check, and successful rollback.",
            "Verify serving observes the alias change and loads the promoted model without a code or image change.",
        ),
    ),
    (
        "05",
        "Canary Delivery, CI/CD & GitOps",
        (
            "Choose and document one canary mechanism (Argo Rollouts is the most direct fit with the existing ArgoCD setup).",
            "Extend serving/helm with stable/canary services and rollout analysis based on errors and latency; begin with a small traffic weight and promote or abort automatically.",
            "Expand .github/workflows/validate.yml to lint all Python, run all tests, validate every Terraform environment, lint Helm, and validate rendered manifests.",
            "Add an image workflow that builds, scans, tags with immutable Git SHA, authenticates using GitHub OIDC, and pushes serving/training images to ECR.",
            "Add a GitOps update step that changes the image tag in environment values; allow ArgoCD to reconcile instead of running kubectl from CI.",
            "Protect production with a GitHub Environment approval and keep registry promotion approval separately auditable.",
            "Verify a failed canary aborts without affecting stable traffic and a passing canary reaches 100%.",
        ),
    ),
    (
        "06",
        "Cloud Infrastructure — Final Pass",
        (
            "Wire the existing S3, RDS, and Redis modules into the environment roots; use smaller dev sizing and production-appropriate retention/HA settings.",
            "Add least-privilege Pod Identity roles and security-group paths for MLflow, Airflow training, Feast/Redis, serving artifact reads, and monitoring.",
            "Deploy MLflow in EKS with RDS/Postgres metadata and S3 artifacts; add probes, resource requests/limits, secrets, and persistent configuration.",
            "Deploy Airflow with DAG delivery, executor configuration, logs, secrets, and access to training nodes and required AWS services.",
            "Deploy Prometheus, Grafana, Alertmanager, and the required ServiceMonitor/PodMonitor resources.",
            "Apply dev first; verify nodes, pods, DNS, TLS, database connectivity, Redis connectivity, S3 writes, and registry model loading before staging/prod.",
            "Record actual cost-relevant resource sizes, capture evidence, then run the documented teardown to stop unnecessary charges.",
        ),
    ),
    (
        "07",
        "End-to-End Verification",
        (
            "Run the feature DAG and prove one point-in-time offline value matches the materialized Redis online value.",
            "Run the training DAG and show validation, MLflow logging, evaluation, and both passing and failing registration gates.",
            "Send a valid /predict request and capture model name/version plus the corresponding Prometheus request and latency metrics.",
            "Inject dramatic drift; capture the Evidently report, Prometheus drift metric, alert, and retraining-DAG trigger.",
            "Show challenger comparison, human approval, new registry version, alias movement, and automatic serving refresh.",
            "Force a post-promotion failure and prove the alias and serving model return to the previous version.",
            "Run the canary and load tests; retain command output and measured throughput/p50/p95/p99 latency.",
        ),
    ),
    (
        "08",
        "Documentation & Portfolio Evidence",
        (
            "Add docs/ARCHITECTURE.md with the data, control, deployment, observability, security, and rollback flows plus a rendered architecture diagram.",
            "Expand README.md with problem/metric, prerequisites, fresh-clone quick start, lifecycle, model results, API, monitoring, costs, trade-offs, and cleanup.",
            "Add dataset source, license/usage terms, data dictionary, versioning method, and reproducibility instructions.",
            "Add docs/PROMOTION_POLICY.md containing exact thresholds, approval responsibility, rollback conditions, and audit fields.",
            "Capture labeled screenshots: cluster, Feast materialization, MLflow runs, registry alias, prediction, drift, Grafana, registration gates, retraining, canary, and rollback.",
            "Record a short demo showing drift → alert → retraining → comparison → approval → promotion → serving refresh → rollback.",
            "Perform a clean-clone rehearsal using only documented commands; fix every missing prerequisite or unstated manual step.",
        ),
    ),
)


def draw_page(canvas: object, document: BaseDocTemplate) -> None:
    """Draw a subtle footer and page number on every content page."""
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#DCE3EA"))
    canvas.line(18 * mm, 14 * mm, 192 * mm, 14 * mm)
    canvas.setFont("DejaVu", 7.5)
    canvas.setFillColor(colors.HexColor("#667085"))
    canvas.drawString(18 * mm, 9 * mm, "PROD-MLOPS-PLATFORM  •  COMPLETION ROADMAP")
    canvas.drawRightString(192 * mm, 9 * mm, f"PAGE {document.page}")
    canvas.restoreState()


def build_pdf(output_path: Path = OUTPUT_PATH) -> Path:
    """Build and return the completion-roadmap PDF path."""
    LOGGER.info("Generating completion roadmap at %s", output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))

    document = BaseDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=17 * mm,
        bottomMargin=20 * mm,
        title="Production MLOps Platform — Implementation Checklist",
        author="prod-mlops-platform",
    )
    document.addPageTemplates(
        PageTemplate(
            id="main",
            frames=[Frame(document.leftMargin, document.bottomMargin, document.width, document.height, id="main-frame")],
            onPage=draw_page,
        )
    )

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontName="DejaVu-Bold", fontSize=27,
        leading=32, textColor=colors.HexColor("#102A43"), alignment=TA_LEFT, spaceAfter=5 * mm,
    )
    kicker = ParagraphStyle(
        "Kicker", parent=styles["Normal"], fontName="DejaVu-Bold", fontSize=9,
        leading=12, textColor=colors.HexColor("#147D92"), tracking=1.4, spaceAfter=3 * mm,
    )
    intro = ParagraphStyle(
        "Intro", parent=styles["Normal"], fontName="DejaVu", fontSize=10.5,
        leading=16, textColor=colors.HexColor("#486581"), spaceAfter=5 * mm,
    )
    section_title = ParagraphStyle(
        "SectionTitle", parent=styles["Heading2"], fontName="DejaVu-Bold", fontSize=14,
        leading=18, textColor=colors.HexColor("#102A43"), spaceAfter=2 * mm,
    )
    item = ParagraphStyle(
        "Item", parent=styles["Normal"], fontName="DejaVu", fontSize=9.5,
        leading=14, textColor=colors.HexColor("#243B53"), leftIndent=1 * mm,
    )
    small = ParagraphStyle(
        "Small", parent=styles["Normal"], fontName="DejaVu", fontSize=8.5,
        leading=12, textColor=colors.HexColor("#52667A"), alignment=TA_CENTER,
    )

    story: list[object] = [
        Spacer(1, 8 * mm),
        Paragraph("PRODUCTION MLOPS PLATFORM", kicker),
        Paragraph("Implementation<br/>Checklist", title),
        Paragraph(
            "A file-level, verifiable checklist for taking the NYC Airbnb pricing platform from its current foundation "
            "to a complete, demonstrated, production-shaped MLOps portfolio project. Each box describes a concrete "
            "deliverable or proof of completion.",
            intro,
        ),
    ]

    summary = Table(
        [
            [Paragraph("CURRENT POSITION", kicker), Paragraph("WORKING CADENCE", kicker), Paragraph("REMAINING CHECKS", kicker)],
            [Paragraph("Foundation implemented", small), Paragraph("16 focused hours/week", small), Paragraph("57 verifiable items", small)],
        ],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF4F6")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#B8DCE3")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9E3E8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ]
        )
    )
    story.extend(
        [
            summary,
            Spacer(1, 5 * mm),
            Paragraph(
                "<b>Already present:</b> feature engineering, Feast definitions, offline/online verification, the "
                "Airflow feature-build DAG, CatBoost training and MLflow logging, registry-alias loading, FastAPI "
                "serving, Docker, Helm, ArgoCD foundations, Terraform modules, validation CI, and unit tests. "
                "The checklist below covers the remaining work.",
                intro,
            ),
            Spacer(1, 3 * mm),
        ]
    )

    for number, heading, tasks in SECTIONS:
        rows: list[list[object]] = []
        for task in tasks:
            rows.append([
                Paragraph("□", ParagraphStyle("Box", parent=item, fontSize=14, leading=14, textColor=colors.HexColor("#147D92"))),
                Paragraph(task, item),
            ])
        task_table = Table(rows, colWidths=[8 * mm, 158 * mm], hAlign="LEFT")
        task_table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 0.5 * mm),
                    ("LINEBELOW", (1, 0), (1, -2), 0.3, colors.HexColor("#E4EAF0")),
                ]
            )
        )
        section_header = Table(
            [[Paragraph(number, kicker), Paragraph(heading, section_title)]],
            colWidths=[14 * mm, 152 * mm],
        )
        section_header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(KeepTogether([section_header, task_table, Spacer(1, 5 * mm)]))

    story.extend(
        [
            Spacer(1, 3 * mm),
            Paragraph("DEFINITION OF DONE", kicker),
            Paragraph(
                "Drift is detected → retraining is triggered → the challenger is evaluated → human approval is recorded → "
                "the registry alias is promoted → serving loads the new version → rollback is proven.",
                ParagraphStyle(
                    "Done", parent=intro, fontName="DejaVu-Bold", fontSize=11,
                    leading=17, textColor=colors.HexColor("#102A43"),
                    borderColor=colors.HexColor("#147D92"), borderWidth=1,
                    borderPadding=5 * mm, backColor=colors.HexColor("#F2F8FA"),
                ),
            ),
        ]
    )
    document.build(story)
    LOGGER.info("Generated %s", output_path)
    return output_path


def main() -> None:
    """Configure logging and generate the PDF."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    build_pdf()


if __name__ == "__main__":
    main()
