import DataTable from "@/components/DataTable";
import { asList, asObject, clean, previewValue } from "@/lib/format.js";
import { EvidenceTerms, FactLabel } from "./EvidenceElements";

const AUTHORITY_LABELS = {
  chapter_title: "챕터 명칭",
  chapter_including: "포함 범위",
  chapter_excluding: "제외 범위",
  allowed_processing_scope: "허용 가공 범위",
  classification_decision_axes: "분류 판단 기준",
  routing_guardrails: "라우팅 제한 기준",
  prepared_food_redirect_chapters: "가공식품 전환 기준",
};

function FactBindingLabel(binding) {
  const source = asObject(binding);
  const path = clean(source.path);
  const field = path.split(".").at(-1);
  const value = previewValue(source.value, 80);
  return [FactLabel(field), value].filter(Boolean).join(": ");
}

export default function TariffRoutingPanel({ result }) {
  const routingView = asObject(result?.routing_view);
  const chapterDetails = asList(routingView.candidate_chapter_details).map((detail, index) => {
    const source = asObject(detail);
    const supportStatus = clean(source.support_status);
    return {
      rank: Number(source.rank) || index + 1,
      chapter: clean(source.chapter),
      status: source.selected
        ? "선택 후보"
        : supportStatus === "supported"
          ? "근거 확인"
          : "추가 검토",
      reason: clean(source.reason) || "Semantic 판단 근거가 기록되지 않았습니다.",
      factEvidence: asList(source.fact_bindings).map(FactBindingLabel).filter(Boolean),
      authorityEvidence: asList(source.authority_bindings)
        .map((field) => AUTHORITY_LABELS[clean(field)] || clean(field).replaceAll("_", " "))
        .filter(Boolean),
    };
  }).slice(0, 3);
  const missingFacts = asList(routingView.missing_facts).map(FactLabel);
  const topChapter = chapterDetails[0];

  return (
    <div className="cjs-panel cjs-routing-panel">
      <div className="cjs-panel-title">챕터 후보 좁히기</div>
      {chapterDetails.length ? (
        <>
          <div className="cjs-routing-summary">
            <div className="cjs-routing-primary-result">
              <span>Semantic 라우팅 결과</span>
              <div><small>HS2</small><strong>{topChapter.chapter}</strong></div>
              <p>{topChapter.reason}</p>
            </div>
          </div>

          {missingFacts.length ? (
            <div className="cjs-routing-warning">
              <strong>추가 확인</strong>
              <span>{missingFacts.join(", ")} 정보가 있으면 후보 비교를 더 정교하게 검토할 수 있습니다.</span>
            </div>
          ) : null}

          <div className="cjs-routing-section-heading">
            <strong>Semantic 후보 순서</strong>
            <span>숫자 점수가 아닌 상품 근거와 챕터 기준의 연결 순서입니다.</span>
          </div>
          <DataTable
            rows={chapterDetails}
            limit={3}
            className="cjs-routing-table"
            columns={[
              { key: "rank", label: "순서", variant: "mono" },
              { key: "chapter", label: "HS2 챕터", variant: "mono" },
              { key: "status", label: "판단 상태" },
              { key: "reason", label: "선택 근거" },
            ]}
          />

          <details className="cjs-secondary-evidence cjs-routing-details">
            <summary>Semantic 판단 근거 보기</summary>
            <div className="cjs-subpanel-title cjs-first">후보별 근거 연결</div>
            <DataTable
              rows={chapterDetails}
              limit={chapterDetails.length}
              className="cjs-routing-detail-table"
              columns={[
                { key: "chapter", label: "HS2 챕터", variant: "mono" },
                { key: "factEvidence", label: "사용한 상품 근거", variant: "chips" },
                { key: "authorityEvidence", label: "참조한 챕터 기준", variant: "chips" },
              ]}
            />

            <div className="cjs-subpanel-title">전체 라우팅 검토 범위</div>
            <EvidenceTerms label="라우팅 검토 범위" terms={routingView.allowed_hs2} />
            <EvidenceTerms label="검토 도메인" terms={routingView.domain_scopes} />
          </details>
        </>
      ) : (
        <div className="cjs-muted">기록된 HS2 후보가 없습니다.</div>
      )}
    </div>
  );
}
