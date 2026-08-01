import { useMemo } from "react";
import { asList, asObject, clean } from "../../../lib/format.js";

function ComponentDone(result, nameFragment) {
  return asList(result?.component_results).some(
    (entry) => clean(entry.component_name).toLowerCase().includes(nameFragment) && entry.success,
  );
}

function EventStatus(result, stageNames) {
  let status = "idle";
  asList(result?.events).forEach((event) => {
    if (stageNames.includes(clean(event.stage))) status = clean(event.status) || status;
  });
  return status;
}

export function NormalizeStageState(status) {
  const value = clean(status).toLowerCase();
  if (["completed", "complete", "done"].includes(value)) return "done";
  if (["needs-review", "needs_review", "review_required"].includes(value)) return "needs-review";
  if (["awaiting-input", "awaiting_input"].includes(value)) return "awaiting-input";
  if (value === "submitting") return "queued";
  if (["idle", "running", "queued", "failed", "skipped"].includes(value)) return value;
  return "idle";
}

export function NormalizeTariffCode(value) {
  return clean(value).replace(/\D/g, "");
}

export function PipelineFailureMessage(value) {
  const raw = clean(value);
  const code = raw.replace(/^[A-Za-z]+Error:\s*/, "").toLowerCase();
  if (code === "staged_classifier_disabled") {
    return "단계별 품목 분류 기능이 비활성화되어 있습니다. 실행 설정을 확인해주세요.";
  }
  if (code === "product_understanding_missing") {
    return "분류에 필요한 상품 이해 결과가 없습니다. 상품 정보를 다시 수집해주세요.";
  }
  if (code === "question_generation_failed") {
    return "분류에 필요한 확인 질문을 만들지 못했습니다. 잠시 후 다시 실행해주세요.";
  }
  if (
    [
      "staged_classifier_unavailable",
      "staged_classifier_exception",
      "staged_no_candidates",
      "invalid_cn8_candidates",
      "no_route_chapters",
    ].includes(code)
    || code.startsWith("no_children_at_")
  ) {
    return "현재 상품 정보로 분류 후보를 생성하지 못했습니다. 입력 정보를 확인한 뒤 다시 실행해주세요.";
  }
  if (/_Component\b/.test(raw)) {
    return "품목 분류를 완료하지 못했습니다. 입력 정보와 실행 상태를 확인해주세요.";
  }
  if (/[가-힣]/.test(raw)) return raw;
  return "품목 분류를 완료하지 못했습니다. 입력 정보와 실행 상태를 확인해주세요.";
}

function PackageMatchLevel(taricKey, group, candidate) {
  const targetTaric10 = NormalizeTariffCode(candidate?.taric10);
  const targetCn8 = NormalizeTariffCode(candidate?.cn8) || targetTaric10.slice(0, 8);
  const targetHs6 = NormalizeTariffCode(candidate?.hs6) || targetCn8.slice(0, 6);
  const packages = asList(group);
  const codes = packages.length ? packages : [{}];

  if (targetTaric10 && codes.some((item) => (
    NormalizeTariffCode(asObject(item).taric10 || taricKey) === targetTaric10
  ))) return "taric10";
  if (targetCn8 && codes.some((item) => {
    const source = asObject(item);
    const taric10 = NormalizeTariffCode(source.taric10 || taricKey);
    return (NormalizeTariffCode(source.cn8) || taric10.slice(0, 8)) === targetCn8;
  })) return "cn8";
  if (targetHs6 && codes.some((item) => {
    const source = asObject(item);
    const taric10 = NormalizeTariffCode(source.taric10 || taricKey);
    const cn8 = NormalizeTariffCode(source.cn8) || taric10.slice(0, 8);
    return (NormalizeTariffCode(source.hs6) || cn8.slice(0, 6)) === targetHs6;
  })) return "hs6";
  return "none";
}

export function BuildDocumentPackageOptions(packagesByTaric, candidate) {
  if (!candidate) return [];
  const entries = Object.entries(asObject(packagesByTaric));
  const candidateId = clean(candidate.candidate_id);
  if (candidateId) {
    const candidateOptions = entries.flatMap(([taric, group]) => {
      const matchingGroup = asList(group).filter(
        (item) => clean(asObject(item).candidate_id) === candidateId,
      );
      if (!matchingGroup.length) return [];
      const matchLevel = PackageMatchLevel(taric, matchingGroup, candidate);
      return [{
        taric,
        group: matchingGroup,
        matchLevel: matchLevel === "none" ? "branch" : matchLevel,
      }];
    });
    if (candidateOptions.length) return candidateOptions;
  }

  const options = entries.map(([taric, group]) => ({
    taric,
    group,
    matchLevel: PackageMatchLevel(taric, group, candidate),
  }));
  const targetCn8 = NormalizeTariffCode(candidate.cn8)
    || NormalizeTariffCode(candidate.taric10).slice(0, 8);
  if (targetCn8) {
    const cn8Branches = options.filter(
      (option) => ["taric10", "cn8"].includes(option.matchLevel),
    );
    if (cn8Branches.length) return cn8Branches;
  }
  const targetHs6 = NormalizeTariffCode(candidate.hs6) || targetCn8.slice(0, 6);
  if (targetHs6) {
    const hs6Branches = options.filter(
      (option) => ["taric10", "cn8", "hs6"].includes(option.matchLevel),
    );
    if (hs6Branches.length) return hs6Branches;
  }
  return options.filter((option) => option.matchLevel === "taric10");
}

export function useClassificationViewModel(result) {
  return useMemo(() => {
    const candidateSet = asObject(result?.candidate_code_set);
    const candidates = asList(candidateSet.candidates);
    const packages = asList(result?.document_packages).slice();
    if (result?.document_package && typeof result.document_package === "object") {
      const packageId = clean(
        result.document_package.document_package_id || result.document_package.taric10,
      );
      if (!packages.some((item) => clean(item.document_package_id || item.taric10) === packageId)) {
        packages.push(result.document_package);
      }
    }
    const packagesByTaric = {};
    packages.forEach((item) => {
      const taric = clean(item.taric10);
      if (taric) (packagesByTaric[taric] = packagesByTaric[taric] || []).push(item);
    });
    return { candidateSet, candidates, packagesByTaric };
  }, [result]);
}

export function GetPipelineStageState(result, viewModel, key) {
  if (key === "product_collection") {
    const event = NormalizeStageState(EventStatus(result, ["Kurly_Product_Collection"]));
    if (event !== "idle") return event;
    return result?.input_processing_view || viewModel.candidates.length ? "done" : "idle";
  }
  if (key === "classification") {
    if (clean(result?.job_status).toLowerCase() === "awaiting_input") {
      return "awaiting-input";
    }
    const event = NormalizeStageState(EventStatus(result, [
      "Input_Intake",
      "Evidence_Intake_Component",
      "Product_Understanding_Component",
      "HS2_Routing_Component",
      "Classification",
      "Classification_Component",
    ]));
    if (event !== "idle") return event;
    if (viewModel.candidates.length) return "done";
    return ComponentDone(result, "classification") ? "done" : "idle";
  }
  if (key === "document_recommendation") {
    const event = NormalizeStageState(EventStatus(result, ["Document_Component"]));
    if (event !== "idle") return event;
    return asList(result?.document_packages).length || result?.document_package ? "done" : "idle";
  }
  return "idle";
}

export function ActiveUserQuestions(result) {
  return asList(result?.user_questions).filter(
    (question) => question?.active && clean(question.user_question_id)
      && clean(question.question_text),
  );
}

export function CompletedPipelineStage(result) {
  const isReconstruction = clean(result?.job_id).startsWith("reconstruct_")
    || asList(result?.events).some(
      (event) => clean(event?.stage) === "Input_Reconstruction",
    );
  return isReconstruction ? "product_collection" : "classification";
}
