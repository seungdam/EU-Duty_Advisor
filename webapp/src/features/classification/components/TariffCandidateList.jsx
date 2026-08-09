import { CheckCircle2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { candidateKey, clean } from "@/lib/format.js";

function CandidateRationale(candidate) {
  const text = clean(candidate?.classification_basis?.[0]);
  return text
    .replace(/^Staged narrowing hs4->hs6->cn8 selected CN8=/, "HS4 → HS6 → CN8 단계 축소: ")
    .replaceAll("_", " ") || "상세 판단 메모를 확인하세요.";
}

function UnresolvedCount(candidate) {
  return new Set([
    ...(Array.isArray(candidate?.required_facts) ? candidate.required_facts : []),
    ...(Array.isArray(candidate?.unknowns) ? candidate.unknowns : []),
  ].map(clean).filter(Boolean)).size;
}

export default function TariffCandidateList({ candidates, selectedKey, onSelect }) {
  if (!candidates.length) {
    return <div className="rounded-xl border bg-surface p-5 text-sm text-muted-foreground">분류 후보가 아직 생성되지 않았습니다.</div>;
  }
  const activeKey = candidates.some((candidate, index) => candidateKey(candidate, index) === selectedKey)
    ? selectedKey
    : "";

  return (
    <section className="min-w-0 rounded-xl border bg-surface shadow-[var(--shadow-surface)]">
      <div className="border-b px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <h3 className="m-0 text-sm font-semibold">분류 후보</h3>
          <Badge variant="secondary">{candidates.length}건</Badge>
        </div>
        <p className="mt-1 mb-0 text-xs leading-5 text-muted-foreground">
          후보를 직접 선택하면 근거가 표시되고 서류 추천 단계가 활성화됩니다.
        </p>
      </div>
      <div className="divide-y">
        {candidates.map((candidate, index) => {
          const key = candidateKey(candidate, index);
          const active = key === activeKey;
          const unresolved = UnresolvedCount(candidate);
          return (
            <button
              type="button"
              key={key}
              className={`block w-full border-l-2 px-4 py-3 text-left transition-colors duration-150 ${active ? "border-l-primary bg-primary/5" : "border-l-transparent hover:bg-muted/70"}`}
              aria-pressed={active}
              onClick={() => onSelect(key)}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2">
                  <strong className="text-sm">{candidate.rank || index + 1}순위</strong>
                  {candidate.llm_recommended ? <Badge className="gap-1"><CheckCircle2 /> 시스템 추천</Badge> : null}
                </span>
              </span>
              <span className="mt-2 grid grid-cols-3 gap-2 text-xs">
                <span><em className="block not-italic text-muted-foreground">HS6</em><b className="font-mono">{clean(candidate.hs6) || "-"}</b></span>
                <span><em className="block not-italic text-muted-foreground">CN8</em><b className="font-mono">{clean(candidate.cn8) || "-"}</b></span>
                <span><em className="block not-italic text-muted-foreground">TARIC10</em><b className="font-mono">{clean(candidate.taric10) || "-"}</b></span>
              </span>
              <span className="mt-2 line-clamp-2 block text-xs leading-5 text-muted-foreground">{CandidateRationale(candidate)}</span>
              <span className={`mt-2 block text-xs font-medium ${unresolved ? "text-needs-review" : "text-success"}`}>
                {unresolved ? `미해결 조건 ${unresolved}건` : "현재 기록된 미해결 조건 없음"}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
