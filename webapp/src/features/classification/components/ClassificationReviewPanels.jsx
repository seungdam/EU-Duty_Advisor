import {
  GRADE_LABELS,
  conditionLabel,
  decisionLabel,
  detailValueText,
  gradeOf,
  isTrueVerdict,
  operationLabel,
  reasonLabel,
  stageEntryForCode,
  summarizeSummons,
  summonsForStage,
  stageLabelKo,
  trueDetails,
  verdictLabel,
} from "@/lib/traceContract.js";
import DataTable from "@/components/DataTable";
import { BuildCandidateHierarchy, UnderstandingValueLabel } from "@/lib/labels.js";
import { asList, asObject, clean } from "@/lib/format.js";
import { PrecedentSummary } from "./EvidenceElements";

function EvidenceGradeBadge({ detail }) {
  const grade = gradeOf(detail);
  if (!grade) return null;
  return <span className={`cjs-grade g-${grade}`}>{GRADE_LABELS[grade] || grade}</span>;
}

function StagePrecedentRow({ row, stageCode }) {
  const joined = row.fired && stageCode && row.code === stageCode;
  return (
    <div className={`cjs-stage-summon ${row.fired ? "fired" : ""}`}>
      <b>📚 이 지점에서 판례 조회</b>
      {row.fired ? (
        <span>
          분류 후보에 반영 — <span className="cjs-mono">{row.code}</span>
          {joined ? " (현재 후보와 일치)" : " (비교 후보로 반영)"}
          {row.refs.length ? ` · ${row.refs.slice(0, 3).join(", ")}` : ""}
        </span>
      ) : (
        <span>
          {row.reviewed || "-"}건 검토 — {row.silenceLabel || "미반영"}
          {row.notInTree ? ` (${row.notInTree})` : ""}
          {row.refs.length ? ` · 근거 판례 ${row.refs.slice(0, 3).join(", ")}` : ""}
        </span>
      )}
      {row.phrases.length ? <em>매치 구문: {row.phrases.slice(0, 3).join(" / ")}</em> : null}
      {row.distribution.length > 1 ? (
        <div className="cjs-summons-bar" title={row.distribution.map((item) => `${item.code} ${item.count}`).join(" · ")}>
          {row.distribution.map((entry) => (
            <span key={entry.code} style={{ flexGrow: Math.max(entry.count, 1) }}>
              {entry.code.slice(-6)} {entry.count}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function RuleTerms(value) {
  const values = Array.isArray(value) ? value : String(value || "").split(/[;,]/);
  return values
    .map((item) => clean(item).replace(/[\[\]'\"]/g, ""))
    .map(UnderstandingValueLabel)
    .filter(Boolean)
    .slice(0, 8);
}

function CandidateRuleSummary({ entry }) {
  const rows = [
    ["일치한 상품 사실", RuleTerms(entry?.matched)],
    ["포함 기준", RuleTerms(entry?.incl)],
    ["배제 기준", RuleTerms(entry?.excl)],
    ["충돌한 사실", RuleTerms(entry?.neg_matched)],
  ].filter(([, values]) => values.length);
  if (!rows.length) return null;
  return (
    <dl className="mt-3 grid gap-2 rounded-lg border bg-surface-muted p-3 text-xs sm:grid-cols-2">
      {rows.map(([label, values]) => (
        <div className="grid gap-1" key={label}>
          <dt className="font-semibold text-muted-foreground">{label}</dt>
          <dd className="m-0 leading-5 text-foreground">{values.join(" · ")}</dd>
        </div>
      ))}
    </dl>
  );
}

function StageEvidenceBlock({ stage, cn8, summons }) {
  const entry = stageEntryForCode(stage, cn8);
  const stageName = clean(asObject(stage).stage);
  const stageSummons = summonsForStage(summons, stageName);
  if (!entry && !stageSummons.length) return null;
  const decision = clean(entry?.decision);
  const positives = entry ? trueDetails(entry) : [];
  const allDetails = entry ? asList(entry.decision_detail) : [];

  return (
    <div className="cjs-stage-trace">
      <div className="cjs-stage-trace-head">
        <b>{stageLabelKo(stageName)}</b>
        {entry ? <span className="cjs-mono">{clean(entry.code)}</span> : null}
        {decision ? <span className={`cjs-decision d-${decision}`}>{decisionLabel(decision)}</span> : null}
        {entry?.residual ? <span className="cjs-residual">기타 항목</span> : null}
      </div>
      <CandidateRuleSummary entry={entry} />
      {stageSummons.map((row, index) => (
        <StagePrecedentRow row={row} stageCode={clean(entry?.code)} key={index} />
      ))}
      {entry ? (
        positives.length ? (
          positives.map((detail, index) => (
            <div className="cjs-trace-detail" key={index}>
              <EvidenceGradeBadge detail={detail} />
              <span className="cjs-mono dim">{conditionLabel(detail.cond)}</span>
              <span>{detailValueText(detail) || reasonLabel(detail.why)}</span>
            </div>
          ))
        ) : (
          <div className="cjs-muted">이 후보를 뒷받침하는 조건이 확인되지 않았습니다.</div>
        )
      ) : null}
      {allDetails.length > positives.length ? (
        <details className="cjs-trace-more">
          <summary>모든 조건 확인 내역 ({allDetails.length}건)</summary>
          {allDetails.map((detail, index) => (
            <div className="cjs-trace-detail full" key={index}>
              <span className={isTrueVerdict(detail) ? "cjs-mono ok" : "cjs-mono dim"}>
                {verdictLabel(detail)}
              </span>
              <span className="cjs-mono dim">
                {conditionLabel(asObject(detail).cond)} · {operationLabel(asObject(detail).op)}
              </span>
              <span>{reasonLabel(asObject(detail).why)}</span>
            </div>
          ))}
        </details>
      ) : null}
    </div>
  );
}

function UnanchoredPrecedents({ summons }) {
  const rows = summarizeSummons(summons).filter((row) => !row.level);
  if (!rows.length) return null;
  return (
    <>
      <div className="cjs-subpanel-title">BTI 판례 조회 이력 (단계 미지정)</div>
      {rows.map((row, index) => (
        <div className="cjs-summons" key={index}>
          <div>
            {row.fired
              ? `분류 후보에 반영: ${row.code}${row.refs.length ? ` — ${row.refs.slice(0, 3).join(", ")}` : ""}`
              : `판례 조회: ${row.reviewed || "-"}건 검토 — ${row.silenceLabel || "미반영"}`}
          </div>
          {row.distribution.length > 1 ? (
            <div className="cjs-summons-bar" title={row.distribution.map((item) => `${item.code} ${item.count}`).join(" · ")}>
              {row.distribution.map((entry) => (
                <span key={entry.code} style={{ flexGrow: Math.max(entry.count, 1) }}>
                  {entry.code.slice(-6)} {entry.count}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </>
  );
}

function ClassificationBasisLabel(value) {
  const basis = clean(value);
  if (basis.startsWith("Staged narrowing")) {
    return basis.replace(
      /^Staged narrowing hs4->hs6->cn8 selected CN8=/,
      "HS4 → HS6 → CN8 단계 축소 결과: ",
    );
  }
  return basis.replaceAll("_", " ");
}

function ClassificationHierarchyNode({ node, selectedCn8 }) {
  const children = asList(node.children);
  const description = ClassificationBasisLabel(node.description);
  const selected = node.level === "CN8" && clean(node.code) === clean(selectedCn8);
  return (
    <li>
      <div className={`cjs-hierarchy-node ${node.recommended ? "recommended" : ""} ${selected ? "selected" : ""}`}>
        <span className="cjs-hierarchy-marker">{node.level}</span>
        <div className="cjs-hierarchy-copy">
          <strong>{node.code}</strong>
          <small>
            {description || `${node.level} ${node.code}의 공식 품목 설명이 결과에 포함되지 않았습니다.`}
          </small>
        </div>
      </div>
      {children.length ? (
        <ul>
          {children.map((child) => (
            <ClassificationHierarchyNode
              node={child}
              selectedCn8={selectedCn8}
              key={`${child.level}-${child.code}`}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function StageCandidateComparison({ trace }) {
  const stages = asList(trace?.stages)
    .filter((stage) => ["hs4", "hs6"].includes(clean(asObject(stage).stage).toLowerCase()))
    .map((stage) => {
      const source = asObject(stage);
      const selected = new Set(asList(source.selected_codes).map(clean).filter(Boolean));
      const considered = asList(source.candidates_considered)
        .map(asObject)
        .filter((candidate) => clean(candidate.code));
      const priority = (candidate) => {
        if (selected.has(clean(candidate.code))) return 0;
        if (clean(candidate.decision) === "confirmed") return 1;
        if (clean(candidate.decision) === "undecided") return 2;
        if (clean(candidate.decision) === "violated") return 4;
        return 3;
      };
      const rows = considered
        .map((candidate, index) => ({ candidate, index }))
        .sort((left, right) => (
          priority(left.candidate) - priority(right.candidate)
          || left.index - right.index
        ))
        .slice(0, 3)
        .map(({ candidate }) => ({
          selected: selected.has(clean(candidate.code)),
          code: clean(candidate.code),
          description: clean(candidate.description)
            || "공식 품목 설명이 trace에 기록되지 않았습니다.",
          decision: decisionLabel(candidate.decision),
        }));
      return {
        stage: clean(source.stage).toLowerCase(),
        rows,
      };
    })
    .filter((stage) => stage.rows.length);

  if (!stages.length) return null;
  return (
    <div className="mt-5 grid gap-4">
      <div>
        <div className="cjs-subpanel-title">단계별 후보 비교</div>
        <div className="cjs-muted">
          True는 이 단계에서 선택된 코드이며, False는 함께 검토된 대안입니다.
        </div>
      </div>
      {stages.map((stage) => (
        <div className="grid gap-2" key={stage.stage}>
          <strong className="text-sm">{stageLabelKo(stage.stage)}</strong>
          <DataTable
            rows={stage.rows}
            limit={3}
            columns={[
              { key: "selected", label: "선택", variant: "pill" },
              { key: "code", label: "코드", variant: "mono" },
              { key: "description", label: "분류표 설명" },
              { key: "decision", label: "판정 상태" },
            ]}
          />
        </div>
      ))}
    </div>
  );
}

export function ClassificationHierarchy({ candidates, selectedPath, selectedCn8, trace }) {
  const tree = BuildCandidateHierarchy(candidates, asObject(selectedPath));
  const hasCandidates = asList(tree.children).length > 0;
  return (
    <div className="cjs-panel cjs-hierarchy-panel">
      <div className="cjs-hierarchy-heading">
        <div>
          <div className="cjs-panel-title">후보 계층 분류 트리</div>
          <small>공통 상위 코드는 묶고, 코드가 갈라지는 지점부터 후보를 들여쓰기했습니다.</small>
        </div>
        <strong>CN8 후보 {asList(candidates).length}개</strong>
      </div>
      {hasCandidates ? (
        <ul className="cjs-hierarchy-tree" aria-label="HS 코드 후보 계층 트리">
          <ClassificationHierarchyNode
            node={tree}
            selectedCn8={selectedCn8}
          />
        </ul>
      ) : (
        <div className="cjs-muted">계층 분류 후보가 기록되지 않았습니다.</div>
      )}
      <StageCandidateComparison trace={trace} />
    </div>
  );
}

export function ClassificationEvidencePanel({ candidate, trace }) {
  if (!candidate) {
    return (
      <div className="cjs-panel">
        <div className="cjs-panel-title">선택 후보 결정 근거</div>
        <div className="cjs-muted">왼쪽 분류 후보를 선택하면 판단 메모와 단계별 근거가 표시됩니다.</div>
      </div>
    );
  }
  const basis = asList(candidate?.classification_basis).map(ClassificationBasisLabel);
  const validator = asObject(trace?.validator);
  const citations = asList(candidate?.classification_citations);
  const validatorCode = clean(validator.cn8 || validator.code || validator.chapter || validator.heading);

  return (
    <div className="cjs-panel">
      <div className="cjs-review-panel-heading">
        <div>
          <div className="cjs-panel-title">선택 후보 결정 근거</div>
          <small>현재 선택한 후보에 연결된 판단 메모와 단계별 근거입니다.</small>
        </div>
        <strong>CN8 {clean(candidate?.cn8) || "미선택"}</strong>
      </div>
      <div className="cjs-subpanel-title cjs-first">판단 메모</div>
      {basis.length ? (
        basis.map((line, index) => <div className="cjs-pill" key={index}>{line}</div>)
      ) : (
        <div className="cjs-muted">표시할 판단 메모가 없습니다.</div>
      )}
      {trace?.hasTrace ? (
        <>
          <div className="cjs-subpanel-title">단계별 분류 근거 · EU 품목분류 판례(BTI) 조회 포함</div>
          {asList(trace.stages).map((stage, index) => (
            <StageEvidenceBlock stage={stage} cn8={candidate?.cn8} summons={trace.summons} key={index} />
          ))}
          {Object.keys(validator).length ? (
            <div className={`cjs-validator ${validator.applied ? "applied" : ""}`}>
              <b>모델 검증 권고</b>
              <span className="cjs-mono">{validatorCode || "코드 미지정"}</span>
              <span>{clean(validator.reason) || "별도 권고 사유가 기록되지 않았습니다."}</span>
              {validator.applied && clean(validator.original_top_cn8) ? (
                <em>적용됨 — 원 1순위 {clean(validator.original_top_cn8)} 교체</em>
              ) : null}
            </div>
          ) : null}
          <UnanchoredPrecedents summons={trace.summons} />
        </>
      ) : (
        <div className="cjs-muted">이 실행에는 단계별 분류 근거가 기록되지 않았습니다.</div>
      )}
      <div className="cjs-subpanel-title">분류에 반영된 BTI 판례 조회</div>
      <div className="cjs-muted">
        {asList(trace?.summons).length
          ? `${asList(trace.summons).length}건의 판례 조회 기록이 있습니다.`
          : "이 실행에는 분류에 반영된 BTI 판례 조회 결과가 없습니다."}
      </div>
      {citations.length ? (
        <div className="cjs-note">
          연결된 인용 {citations.length}건은 TARIC 분기 조회 출처입니다. CN8 법적 확정 근거로 해석하지 않습니다.
        </div>
      ) : null}
      <details className="cjs-secondary-evidence">
        <summary>
          유사 EU 분류 판례 {asList(candidate?.similar_ebti_cases).length}건
          (참고 전용 · 후보 선택에 무관여)
        </summary>
        <PrecedentSummary cases={candidate?.similar_ebti_cases} />
      </details>
    </div>
  );
}
