import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import DocumentPackageDetail from "@/components/DocumentPackageDetail";
import WorkspaceHeader from "@/components/layout/WorkspaceHeader";
import { getJson } from "@/lib/api.js";
import { asList, clean } from "@/lib/format.js";

export default function DocumentPage() {
  const { jobId, taric10 } = useParams();
  const navigate = useNavigate();
  const returnTarget = `/classification?job=${encodeURIComponent(jobId)}`;
  const [packages, setPackages] = useState([]);
  const [packageData, setPackageData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const selectedCandidateId = clean(packageData?.candidate_id);
  const selectedCn8 = clean(packageData?.cn8);
  const candidatePackages = selectedCandidateId
    ? packages.filter((pkg) => clean(pkg.candidate_id) === selectedCandidateId)
    : [];
  const cn8Packages = selectedCn8
    ? packages.filter((pkg) => clean(pkg.cn8) === selectedCn8)
    : [];
  const visiblePackages = candidatePackages.length
    ? candidatePackages
    : cn8Packages.length
      ? cn8Packages
      : packages;

  const PackagePath = (target) =>
    `/document/${encodeURIComponent(jobId)}/${encodeURIComponent(target)}`;

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError("");
      setPackageData(null);
      try {
        const detail = await getJson(
          `/api/runs/${encodeURIComponent(jobId)}/document-packages/${encodeURIComponent(taric10)}`,
        );
        if (!cancelled) {
          setPackageData(detail.document_package || detail);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(String(loadError?.message || loadError));
        }
      }
      try {
        const collection = await getJson(
          `/api/runs/${encodeURIComponent(jobId)}/document-packages`,
        );
        if (!cancelled) {
          setPackages(asList(collection.packages));
        }
      } catch {
        /* 목록은 보조 정보 — 실패해도 상세 렌더는 유지 */
      }
      if (!cancelled) {
        setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [jobId, taric10]);

  return (
    <div className="grid min-w-0 gap-5">
      <Link className="w-fit text-sm font-semibold text-primary hover:underline" to={returnTarget}>
        ← 품목 분류
      </Link>
      <WorkspaceHeader
        eyebrow="EU Import Documents"
        title="TARIC 상세 서류 추천"
        description="선택한 TARIC10에 적용될 수 있는 서류와 확인 조건을 검토합니다. 최종 제출 요건은 관할기관 또는 전문가의 확인이 필요합니다."
      />

      {visiblePackages.length > 1 ? (
        <section className="flex flex-col gap-3 rounded-xl border bg-surface px-4 py-4 shadow-[var(--shadow-surface)] sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <div className="min-w-0">
            <h2 className="m-0 text-base font-semibold text-foreground">TARIC Branch 선택</h2>
            <p className="mt-1 mb-0 text-sm leading-6 text-muted-foreground">
              선택 후보의 CN8 {selectedCn8 || "-"}에 연결된 {visiblePackages.length}개 Branch입니다.
            </p>
          </div>
          <Select value={clean(packageData?.taric10) || taric10} onValueChange={(target) => navigate(PackagePath(target))}>
            <SelectTrigger className="w-full sm:w-[320px]" aria-label="TARIC 서류 패키지 선택">
              <SelectValue placeholder="TARIC10 선택" />
            </SelectTrigger>
            <SelectContent>
              {visiblePackages.map((pkg, index) => {
                const target = clean(pkg.taric10 || pkg.document_package_id);
                const branchIndex = Number(pkg.taric10_branch_index) || index + 1;
                const suffix = pkg.taric10_is_recommended ? " · 추천" : "";
                return (
                  <SelectItem value={target} key={clean(pkg.document_package_id) || target}>
                    Branch {branchIndex} · {target}{suffix}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </section>
      ) : null}

      {error ? (
        <Alert variant="destructive">
          <AlertTitle>서류 패키지를 불러오지 못했습니다.</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      {loading && !packageData ? (
        <div className="grid gap-3 rounded-xl border bg-surface p-6" role="status" aria-label="서류 추천 데이터를 불러오는 중">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : null}
      {!loading && !error && !packageData ? (
        <Alert><AlertTitle>표시할 서류 패키지가 없습니다.</AlertTitle></Alert>
      ) : null}
      {packageData ? <DocumentPackageDetail packageData={packageData} /> : null}
    </div>
  );
}
