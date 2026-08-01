import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getJson } from "../lib/api.js";
import { JOB_STORAGE_KEY } from "../hooks/useClassificationRun";
import { asList, asObject, clean, isFilled, previewValue } from "../lib/format.js";

const RECONSTRUCTION_STATUS_LABELS = {
  mode: "복원 방식",
  used_llm_reconstruction: "LLM 복원 사용",
  fallback_reason: "대체 사유",
  error: "오류",
  detail_table_count: "복원 상세표 수",
  classification_fact_count: "구조화 fact 수",
  classification_text_line_count: "정규화 문장 수",
};

const PU_FIELDS = [
  ["understanding_id", "Understanding ID"],
  ["product_id", "Product ID"],
  ["product_name", "상품명"],
  ["short_description", "짧은 설명"],
  ["classification_text", "분류 입력 통합 텍스트"],
  ["reconstructed_fact_texts", "LLM 복원 정규화 텍스트"],
  ["reconstructed_product_facts", "LLM 복원 구조화 fact"],
  ["distilled_identity", "Distilled identity (wiki seed)"],
  ["identity_hints", "Identity hints (HS2-HS4)"],
  ["composition_facts", "Composition facts (HS6-CN8)"],
  ["coi_evidence", "COI 보조 근거"],
  ["encyclopedia_evidence", "백과사전 보조 근거"],
  ["routing_terms", "라우팅 입력 토큰"],
  ["blocked_routing_terms", "라우팅 제외 토큰"],
  ["excluded_from_routing_terms", "라우팅에서 빠진 토큰"],
  ["unknowns", "부족한 입력 정보"],
];

function KeyValueList({ entries }) {
  if (!entries.length) {
    return <div className="cadm-muted">데이터가 없습니다.</div>;
  }
  return entries.map(([label, value]) => (
    <div className="cadm-kv" key={label}>
      <span>{label}</span>
      <strong>{previewValue(value)}</strong>
    </div>
  ));
}

function RecordTable({ records, limit = 60 }) {
  const rows = asList(records).slice(0, limit);
  if (!rows.length) {
    return <div className="cadm-muted">레코드가 없습니다.</div>;
  }
  const columns = [];
  rows.forEach((row) => {
    Object.keys(asObject(row)).forEach((key) => {
      if (!columns.includes(key)) {
        columns.push(key);
      }
    });
  });
  return (
    <div className="cadm-table-scroll">
      <table className="cadm-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>{previewValue(asObject(row)[column], 200)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TextList({ items, limit = 60 }) {
  const list = asList(items).slice(0, limit);
  if (!list.length) {
    return <div className="cadm-muted">항목이 없습니다.</div>;
  }
  return (
    <ol className="cadm-text-list">
      {list.map((item, index) => (
        <li key={index}>{previewValue(item, 500)}</li>
      ))}
    </ol>
  );
}

export default function AdminPage() {
  const [searchParams] = useSearchParams();
  const [jobIdInput, setJobIdInput] = useState("");
  const [snapshot, setSnapshot] = useState(null);
  const [blackboard, setBlackboard] = useState(null);
  const [status, setStatus] = useState({ kind: "info", message: "" });
  const [loading, setLoading] = useState(false);

  const resolveJobId = useCallback(() => {
    if (clean(jobIdInput)) {
      return clean(jobIdInput);
    }
    if (clean(searchParams.get("job"))) {
      return clean(searchParams.get("job"));
    }
    try {
      return clean(window.sessionStorage.getItem(JOB_STORAGE_KEY));
    } catch {
      return "";
    }
  }, [jobIdInput, searchParams]);

  const load = useCallback(async () => {
    if (loading) {
      return;
    }
    const jobId = resolveJobId();
    if (!jobId) {
      setStatus({ kind: "warn", message: "job_id가 없습니다. 분류 페이지에서 실행 후 다시 열거나 직접 입력하세요." });
      return;
    }
    setJobIdInput(jobId);
    setLoading(true);
    setStatus({ kind: "info", message: `${jobId} 불러오는 중…` });
    let snapshotError = "";
    let blackboardError = "";
    let nextSnapshot = null;
    let nextBlackboard = null;
    try {
      nextSnapshot = await getJson(`/api/runs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      snapshotError = String(error?.message || error);
    }
    try {
      nextBlackboard = await getJson(`/api/admin/runs/${encodeURIComponent(jobId)}/blackboard`);
    } catch (error) {
      blackboardError = String(error?.message || error);
    }
    setSnapshot(nextSnapshot);
    setBlackboard(nextBlackboard);
    setLoading(false);
    if (snapshotError && blackboardError) {
      setStatus({ kind: "error", message: `불러오기 실패 — snapshot: ${snapshotError} / blackboard: ${blackboardError}` });
    } else if (snapshotError) {
      setStatus({ kind: "warn", message: "실행 스냅샷 없음(서버 재시작?) — blackboard 아티팩트만 표시합니다." });
    } else if (blackboardError) {
      setStatus({ kind: "warn", message: `blackboard 아티팩트 없음 — 스냅샷 정보만 표시합니다. (${blackboardError})` });
    } else {
      setStatus({
        kind: "ok",
        message: `${jobId} 로드 완료 · 상태 ${clean(nextSnapshot?.job_status) || "unknown"}`,
      });
    }
  }, [loading, resolveJobId]);

  useEffect(() => {
    if (resolveJobId()) {
      load();
    } else {
      setStatus({ kind: "info", message: "job_id를 입력하거나 분류 페이지에서 실행 후 접속하세요." });
    }
    // 최초 진입 시 1회만 자동 로드
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const inputView = asObject(snapshot?.input_processing_view);
  const reconstructionStatus = asObject(inputView.reconstruction_status);
  const usedLlm = !!reconstructionStatus.used_llm_reconstruction;
  const facts = asList(inputView.reconstructed_product_facts);
  const texts = asList(inputView.reconstructed_fact_texts);
  const unresolved = asList(inputView.unresolved_product_facts);
  const conflicts = asList(inputView.product_fact_conflicts);
  const pu = asObject(blackboard?.product_understanding);
  const puFilledCount = PU_FIELDS.filter(([key]) => isFilled(pu[key])).length;

  const statusEntries = Object.keys(RECONSTRUCTION_STATUS_LABELS)
    .filter(
      (key) =>
        reconstructionStatus[key] !== undefined &&
        reconstructionStatus[key] !== null &&
        String(reconstructionStatus[key]) !== "",
    )
    .map((key) => [RECONSTRUCTION_STATUS_LABELS[key], reconstructionStatus[key]]);

  return (
    <div className="classification-admin-shell">
      <div className="cadm-hero">
        <div className="cadm-eyebrow">ASAP Admin</div>
        <h1 className="cadm-title">Run Inspector</h1>
        <div className="cadm-subtitle">
          LLM reconstruction 결과물과 ProductUnderstandingPackage가 어떤 값으로 채워졌는지 확인합니다.
        </div>
      </div>

      <section className="cadm-controls">
        <div className="cadm-field">
          <label htmlFor="cadm-job-id">작업 번호 (job_id)</label>
          <input
            id="cadm-job-id"
            type="text"
            className="cadm-input"
            placeholder="예: job_ab12cd34ef — 비우면 최근 실행을 불러옵니다"
            value={jobIdInput}
            onChange={(event) => setJobIdInput(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && load()}
          />
        </div>
        <button type="button" className="cadm-primary-button" onClick={load} disabled={loading}>
          불러오기
        </button>
        <div className="cadm-status">
          {status.message ? (
            <span className={`cadm-status-chip ${status.kind}`}>{status.message}</span>
          ) : null}
        </div>
      </section>

      <section className="cadm-section">
        <div className="cadm-panel">
          <div className="cadm-panel-title">
            Reconstruction 상태
            <span className={`cadm-badge ${usedLlm ? "ok" : "warn"}`}>
              {usedLlm ? "LLM 복원 수행" : "LLM 복원 미수행"}
            </span>
          </div>
          <KeyValueList entries={statusEntries} />
        </div>
        <div className="cadm-panel">
          <div className="cadm-panel-title">
            복원된 구조화 fact
            <span className={`cadm-badge ${facts.length ? "ok" : "warn"}`}>{facts.length}건</span>
          </div>
          <RecordTable records={facts} limit={80} />
        </div>
        <div className="cadm-panel">
          <div className="cadm-panel-title">
            복원 정규화 텍스트
            <span className={`cadm-badge ${texts.length ? "ok" : "warn"}`}>{texts.length}줄</span>
          </div>
          <TextList items={texts} limit={80} />
        </div>
        <div className="cadm-panel">
          <div className="cadm-panel-title">
            미해결 fact / 충돌
            <span className={`cadm-badge ${unresolved.length || conflicts.length ? "warn" : "ok"}`}>
              미해결 {unresolved.length} · 충돌 {conflicts.length}
            </span>
          </div>
          <div className="cadm-subpanel-title">미해결 fact</div>
          <RecordTable records={unresolved} limit={40} />
          <div className="cadm-subpanel-title">충돌</div>
          <TextList items={conflicts} limit={20} />
        </div>
      </section>

      <section className="cadm-section">
        <div className="cadm-panel cadm-panel-wide">
          <div className="cadm-panel-title">
            ProductUnderstandingPackage 채움 현황
            <span className={`cadm-badge ${puFilledCount === PU_FIELDS.length ? "ok" : "warn"}`}>
              {puFilledCount}/{PU_FIELDS.length} 채워짐
            </span>
          </div>
          {Object.keys(pu).length ? (
            <div className="cadm-fill-grid">
              {PU_FIELDS.map(([key, label]) => {
                const value = pu[key];
                const filled = isFilled(value);
                const count = Array.isArray(value)
                  ? `${value.length}개`
                  : value && typeof value === "object"
                    ? `${Object.keys(value).length}필드`
                    : "";
                return (
                  <div className={`cadm-fill-row ${filled ? "filled" : "empty"}`} key={key}>
                    <span className="cadm-fill-dot" />
                    <div className="cadm-fill-main">
                      <div className="cadm-fill-head">
                        <strong>{label}</strong>
                        <code>{key}</code>
                        {count ? <em>{count}</em> : null}
                      </div>
                      <div className="cadm-fill-preview">
                        {filled ? previewValue(value, 420) : "비어 있음"}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="cadm-muted">
              blackboard에서 product_understanding을 찾지 못했습니다. 파이프라인이 ProductUnderstanding
              단계까지 진행됐는지 확인하세요.
            </div>
          )}
        </div>
      </section>

      {/* 분류 판정 디버그 — 소비자에겐 숨기는 라우터·게이트 내부 관측 (기록 의무화 계약) */}
      <section className="cadm-section">
        <div className="cadm-panel cadm-panel-wide">
          <div className="cadm-panel-title">분류 판정 디버그 (trace)</div>
          {(() => {
            const sets = asList(blackboard?.candidate_code_sets);
            const lastSet = asObject(sets[sets.length - 1]);
            const routing = asObject(blackboard?.routing_context);
            const chapterDetails = asList(routing.candidate_chapter_details);
            const mergeGates = asList(lastSet.merge_gate_observations);
            const trustGates = asList(lastSet.router_trust_gate);
            const formHits = asList(lastSet.form_hits);
            if (!chapterDetails.length && !mergeGates.length && !trustGates.length && !formHits.length) {
              return <div className="cadm-muted">trace 관측 기록이 없습니다 (기록 의무화 이후 run부터 표시).</div>;
            }
            return (
              <>
                {chapterDetails.length ? (
                  <>
                    {/* 라우터 서명(absorbed:/dict:/universal_scope_muted:)은 가공 없이 그대로 노출 */}
                    <div className="cadm-subpanel-title">챕터 서열 · 매치 어휘 (routing_context)</div>
                    <RecordTable records={chapterDetails} limit={20} />
                  </>
                ) : null}
                {trustGates.length ? (
                  <>
                    <div className="cadm-subpanel-title">router_trust_gate</div>
                    <RecordTable records={trustGates} limit={20} />
                  </>
                ) : null}
                {mergeGates.length ? (
                  <>
                    <div className="cadm-subpanel-title">merge_gate_observations</div>
                    <RecordTable records={mergeGates} limit={20} />
                  </>
                ) : null}
                {formHits.length ? (
                  <>
                    <div className="cadm-subpanel-title">form_hits</div>
                    <RecordTable records={formHits} limit={20} />
                  </>
                ) : null}
                {/* [슬롯] 10회차 Validator 대조군 — pipeline pick vs LLM 대조군 pick 나란히 + 일치 뱃지.
                    반드시 "대조군은 결정에 무관여" 라벨과 함께 붙일 것. */}
              </>
            );
          })()}
        </div>
      </section>

      <section className="cadm-section">
        <details className="cadm-panel">
          <summary>Raw · input_processing_view</summary>
          <pre className="cadm-json">{JSON.stringify(inputView, null, 2)}</pre>
        </details>
        <details className="cadm-panel">
          <summary>Raw · product_evidence_state</summary>
          <pre className="cadm-json">
            {JSON.stringify(asObject(blackboard?.product_evidence_state), null, 2)}
          </pre>
        </details>
        <details className="cadm-panel">
          <summary>Raw · product_understanding</summary>
          <pre className="cadm-json">{JSON.stringify(pu, null, 2)}</pre>
        </details>
      </section>
    </div>
  );
}
