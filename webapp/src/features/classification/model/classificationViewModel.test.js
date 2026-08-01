import assert from "node:assert/strict";
import test from "node:test";
import {
  BuildDocumentPackageOptions,
  ActiveUserQuestions,
  CompletedPipelineStage,
  GetPipelineStageState,
  NormalizeStageState,
  NormalizeTariffCode,
  PipelineFailureMessage,
} from "./classificationViewModel.js";

test("실행 이벤트와 결과 DTO를 파이프라인 표시 상태로 변환한다", () => {
  const emptyViewModel = { candidates: [] };
  assert.equal(
    GetPipelineStageState(
      { events: [{ stage: "HS2_Routing_Component", status: "running" }] },
      emptyViewModel,
      "classification",
    ),
    "running",
  );
  assert.equal(
    GetPipelineStageState({}, { candidates: [{ cn8: "19023010" }] }, "classification"),
    "done",
  );
  assert.equal(
    GetPipelineStageState({ document_packages: [{ taric10: "1902301010" }] }, emptyViewModel, "document_recommendation"),
    "done",
  );
});

test("분류 단계는 최신 이벤트를 후보 fallback보다 우선한다", () => {
  const emptyViewModel = { candidates: [] };
  const candidateViewModel = { candidates: [{ cn8: "19023010" }] };
  const StageState = (events, viewModel = candidateViewModel) => (
    GetPipelineStageState({ events }, viewModel, "classification")
  );

  assert.equal(StageState([], emptyViewModel), "idle");
  assert.equal(StageState([]), "done");
  assert.equal(StageState([{ stage: "Classification_Component", status: "running" }]), "running");
  assert.equal(StageState([{ stage: "Classification_Component", status: "needs-review" }]), "needs-review");
  assert.equal(StageState([
    { stage: "Classification_Component", status: "needs-review" },
    { stage: "Classification_Component", status: "done" },
  ]), "done");
  assert.equal(StageState([{ stage: "Classification_Component", status: "failed" }]), "failed");
  assert.equal(StageState([{ stage: "Classification_Component", status: "needs_review" }]), "needs-review");
  assert.equal(StageState([{ stage: "Classification_Component", status: "review_required" }]), "needs-review");
});

test("입력 복원 완료 결과는 상품 정보 수집 단계에 머문다", () => {
  assert.equal(
    CompletedPipelineStage({
      job_id: "reconstruct_123",
      events: [{ stage: "Input_Reconstruction", status: "completed" }],
    }),
    "product_collection",
  );
  assert.equal(CompletedPipelineStage({ job_id: "job_123" }), "classification");
});

test("needs-review 파이프라인 상태와 alias를 정규화한다", () => {
  assert.equal(NormalizeStageState("needs-review"), "needs-review");
  assert.equal(NormalizeStageState("needs_review"), "needs-review");
  assert.equal(NormalizeStageState("review_required"), "needs-review");
  assert.equal(NormalizeStageState("unexpected"), "idle");
  assert.equal(NormalizeStageState(null), "idle");
  assert.equal(NormalizeStageState(undefined), "idle");
  assert.equal(
    GetPipelineStageState(
      { events: [{ stage: "Document_Component", status: "review_required" }] },
      { candidates: [] },
      "document_recommendation",
    ),
    "needs-review",
  );
});

test("사용자 응답 대기 상태와 활성 질문만 UI에 전달한다", () => {
  assert.equal(NormalizeStageState("awaiting_input"), "awaiting-input");
  assert.equal(
    GetPipelineStageState(
      { job_status: "awaiting_input" },
      { candidates: [] },
      "classification",
    ),
    "awaiting-input",
  );
  assert.deepEqual(
    ActiveUserQuestions({
      user_questions: [
        { user_question_id: "uq_1", question_text: "질문 1", active: true },
        { user_question_id: "uq_2", question_text: "질문 2", active: false },
      ],
    }).map((question) => question.user_question_id),
    ["uq_1"],
  );
});

test("관세 코드는 구분 기호를 제거해 비교한다", () => {
  assert.equal(NormalizeTariffCode("1605 55-00 00"), "1605550000");
});

test("내부 분류 실패 코드를 사용자 문장으로 변환한다", () => {
  assert.equal(
    PipelineFailureMessage("RuntimeError: no_children_at_hs4"),
    "현재 상품 정보로 분류 후보를 생성하지 못했습니다. 입력 정보를 확인한 뒤 다시 실행해주세요.",
  );
  assert.equal(
    PipelineFailureMessage("staged_classifier_unavailable"),
    "현재 상품 정보로 분류 후보를 생성하지 못했습니다. 입력 정보를 확인한 뒤 다시 실행해주세요.",
  );
  assert.equal(
    PipelineFailureMessage("사용자 입력값을 확인해주세요."),
    "사용자 입력값을 확인해주세요.",
  );
  assert.equal(
    PipelineFailureMessage("unexpected internal failure"),
    "품목 분류를 완료하지 못했습니다. 입력 정보와 실행 상태를 확인해주세요.",
  );
  assert.equal(
    PipelineFailureMessage("Classification_Component 실패"),
    "품목 분류를 완료하지 못했습니다. 입력 정보와 실행 상태를 확인해주세요.",
  );
});

test("candidate_id가 없으면 선택 후보의 CN8 Branch를 우선 반환한다", () => {
  const packages = {
    "1605550000": [{ taric10: "1605 55 00 00" }],
    "1605550090": [{ cn8: "16055500" }],
    "1605590000": [{ hs6: "160555" }],
    "2106900000": [{ taric10: "2106900000" }],
  };
  const options = BuildDocumentPackageOptions(packages, {
    hs6: "160555",
    cn8: "16055500",
    taric10: "1605550000",
  });

  assert.deepEqual(options.map((option) => option.matchLevel), [
    "taric10",
    "cn8",
  ]);
  assert.equal(options.length, 2);
});

test("직접 매칭이 없으면 임의 패키지를 선택하지 않는다", () => {
  const options = BuildDocumentPackageOptions(
    { "2106900000": [{ taric10: "2106900000" }] },
    { taric10: "1605550000" },
  );
  assert.equal(options.length, 0);
});

test("candidate_id로 선택 후보의 TARIC Branch만 남긴다", () => {
  const options = BuildDocumentPackageOptions(
    {
      "1601009919": [{ candidate_id: "cand_001", taric10: "1601009919" }],
      "1601009999": [{ candidate_id: "cand_001", taric10: "1601009999" }],
      "1902199090": [{ candidate_id: "cand_002", taric10: "1902199090" }],
    },
    {
      candidate_id: "cand_001",
      cn8: "16010099",
      taric10: "1601009999",
    },
  );

  assert.deepEqual(options.map((option) => option.taric), ["1601009919", "1601009999"]);
});
