import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useSearchParams } from "react-router-dom";
import WorkspaceHeader from "@/components/layout/WorkspaceHeader";
import {
  ClassificationPager,
  ClassificationStageHeader,
} from "@/features/classification/components/ClassificationNavigation";
import {
  ClassificationEvidencePanel,
  ClassificationHierarchy,
} from "@/features/classification/components/ClassificationReviewPanels";
import DocumentRecommendationPanel from "@/features/classification/components/DocumentRecommendationPanel";
import PipelineStageRail from "@/features/classification/components/PipelineStageRail";
import ProductInputPanel from "@/features/classification/components/ProductInputPanel";
import {
  ProductCollectionPanel,
  ProductUnderstandingPanel,
} from "@/features/classification/components/ProductEvidencePanels";
import TariffCandidateList from "@/features/classification/components/TariffCandidateList";
import TariffRoutingPanel from "@/features/classification/components/TariffRoutingPanel";
import UserQuestionPanel from "@/features/classification/components/UserQuestionPanel";
import {
  ActiveUserQuestions,
  CompletedPipelineStage,
  GetPipelineStageState,
  useClassificationViewModel,
} from "@/features/classification/model/classificationViewModel.js";
import { useClassificationRun } from "@/hooks/useClassificationRun";
import { ClassificationStepForResult, STAGES } from "@/lib/labels.js";
import { candidateKey, clean } from "@/lib/format.js";
import { extractTrace } from "@/lib/traceContract.js";

export default function WorkbenchPage() {
  const reduceMotion = useReducedMotion();
  const [searchParams, setSearchParams] = useSearchParams();
  const jobQuery = clean(searchParams.get("job"));
  const {
    result,
    busy,
    restoring,
    answering,
    answerError,
    restoreError,
    restorableJobId,
    runPipeline,
    answerQuestions,
    loadRun,
    clearRestoreError,
  } = useClassificationRun(jobQuery);
  const [selectedKey, setSelectedKey] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [activeStage, setActiveStage] = useState("classification");
  const [classificationStep, setClassificationStep] = useState("understanding");
  const followStageRef = useRef(true);
  const followClassificationRef = useRef(true);
  const viewModel = useClassificationViewModel(result);
  const activeQuestions = ActiveUserQuestions(result);
  const defaultCandidate = viewModel.candidates.find((candidate) => candidate.llm_recommended)
    || viewModel.candidates[0]
    || null;
  const resultJobId = clean(result?.job_id || result?.run_id);
  const selectedCandidate = selectedJobId === resultJobId
    ? viewModel.candidates.find(
      (candidate, index) => candidateKey(candidate, index) === selectedKey,
    ) || null
    : null;
  const trace = useMemo(
    () => extractTrace(viewModel.candidateSet),
    [viewModel.candidateSet],
  );

  useEffect(() => {
    if (!restorableJobId || jobQuery) return;
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("job", restorableJobId);
    setSearchParams(nextParams, { replace: true });
  }, [jobQuery, restorableJobId, searchParams, setSearchParams]);

  useEffect(() => {
    if (busy) {
      followStageRef.current = true;
      followClassificationRef.current = true;
    }
  }, [busy]);

  useEffect(() => {
    setSelectedKey("");
    setSelectedJobId("");
    setActiveStage((current) => (
      current === "document_recommendation" ? "classification" : current
    ));
  }, [resultJobId]);

  useEffect(() => {
    if (!followStageRef.current) return;
    const status = clean(result?.job_status).toLowerCase();
    if (["completed", "complete", "done"].includes(status)) {
      const completedStage = CompletedPipelineStage(result);
      setActiveStage(
        completedStage === "document_recommendation" && !selectedCandidate
          ? "classification"
          : completedStage,
      );
      return;
    }
    let latest = "product_collection";
    STAGES.forEach(([key]) => {
      const state = GetPipelineStageState(result, viewModel, key);
      if (["done", "running", "completed", "awaiting-input"].includes(state)) latest = key;
    });
    if (latest === "document_recommendation" && !selectedCandidate) {
      latest = "classification";
    }
    setActiveStage(latest);
  }, [result, viewModel, busy, selectedCandidate]);

  useEffect(() => {
    if (followClassificationRef.current) {
      setClassificationStep(ClassificationStepForResult(result));
    }
  }, [result]);

  const PickStage = (key) => {
    if (key === "document_recommendation" && !selectedCandidate) return;
    followStageRef.current = false;
    setActiveStage(key);
  };

  const PickClassificationStep = (key) => {
    followStageRef.current = false;
    followClassificationRef.current = false;
    setClassificationStep(key);
  };

  const SelectCandidate = (key) => {
    followStageRef.current = false;
    setSelectedJobId(resultJobId);
    setSelectedKey(key);
  };

  const RestoreRun = async (jobId) => {
    const snapshot = await loadRun(jobId);
    if (snapshot) {
      followStageRef.current = true;
      const restoredJobId = clean(snapshot?.job_id) || clean(jobId);
      if (restoredJobId && restoredJobId !== jobQuery) {
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set("job", restoredJobId);
        setSearchParams(nextParams, { replace: true });
      }
    }
    return snapshot;
  };

  const StartRun = async (mode, form) => {
    const snapshot = await runPipeline(mode, form);
    const nextJobId = clean(snapshot?.job_id);
    if (nextJobId && nextJobId !== jobQuery) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.set("job", nextJobId);
      setSearchParams(nextParams, { replace: true });
    }
    return snapshot;
  };

  const SubmitQuestionAnswers = async (answers) => {
    const snapshot = await answerQuestions(answers);
    if (snapshot) {
      followStageRef.current = false;
      followClassificationRef.current = false;
      setActiveStage("classification");
      setClassificationStep("hierarchy");
    }
    return snapshot;
  };

  const resultStatus = clean(result?.job_status).toLowerCase();
  const hasRun = Boolean(
    clean(result?.job_id)
    || result?.error
    || ["submitting", "queued", "running", "awaiting_input", "completed", "complete", "done"].includes(resultStatus)
    || result?.input_processing_view
    || viewModel.candidates.length,
  );

  return (
    <div className="classification-js-shell grid min-w-0 gap-4">
      <div className={hasRun ? "" : "mx-auto w-full max-w-[1040px]"}>
        <WorkspaceHeader
          eyebrow="ASAP Classification"
          title="EU 수출 품목분류"
          description="상품 정보를 읽고 HS6/CN8 후보와 TARIC10 서류 연결 지점을 확인합니다."
          badge="분류 워크스페이스"
        />
      </div>

      {restoring ? (
        <div className="rounded-lg border bg-surface px-4 py-3 text-sm text-muted-foreground" role="status">
          요청한 작업의 snapshot을 확인하는 중입니다. 현재 작업은 전환이 확정될 때까지 유지됩니다.
        </div>
      ) : null}
      {restoreError && hasRun ? (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm" role="alert">
          <span>{restoreError}</span>
          <button className="shrink-0 font-medium text-muted-foreground hover:text-foreground" type="button" onClick={clearRestoreError}>
            닫기
          </button>
        </div>
      ) : null}

      <div className={hasRun ? "grid min-w-0 gap-4 lg:grid-cols-[minmax(250px,300px)_minmax(0,1fr)]" : "mx-auto w-full max-w-[1040px]"}>
        <ProductInputPanel
          busy={busy}
          result={result}
          restoreError={restoreError}
          onRun={StartRun}
          onRestore={RestoreRun}
          compact={hasRun}
        />

        {hasRun ? (
          <section className="grid min-w-0 content-start gap-4">
            <PipelineStageRail
              result={result}
              viewModel={viewModel}
              activeStage={activeStage}
              onSelect={PickStage}
              busy={busy}
              restoring={restoring}
              documentReady={Boolean(selectedCandidate)}
            />
            <main className="grid min-w-0 content-start gap-4">
          {activeStage === "product_collection" ? (
            <ProductCollectionPanel result={result} />
          ) : null}
          {activeStage === "classification" ? (
            <>
              <ClassificationStageHeader
                activeStep={classificationStep}
                onSelect={PickClassificationStep}
              />
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={classificationStep}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: reduceMotion ? 0 : 0.18, ease: "easeOut" }}
                  className="grid min-w-0 gap-4"
                >
                {classificationStep === "understanding" ? (
                  <ProductUnderstandingPanel result={result} />
                ) : null}
                {classificationStep === "routing" ? (
                  <TariffRoutingPanel result={result} />
                ) : null}
                {classificationStep === "hierarchy" ? (
                  <>
                    {activeQuestions.length && resultStatus === "awaiting_input" ? (
                      <UserQuestionPanel
                        key={resultJobId}
                        questions={activeQuestions}
                        onSubmit={SubmitQuestionAnswers}
                        submitting={answering}
                        error={answerError}
                      />
                    ) : null}
                    {viewModel.candidates.length ? (
                      <ClassificationHierarchy
                        candidates={viewModel.candidates}
                        selectedPath={viewModel.candidateSet.selected_path}
                        selectedCn8={selectedCandidate?.cn8 || defaultCandidate?.cn8}
                        trace={trace}
                      />
                    ) : null}
                  </>
                ) : null}
                {classificationStep === "review" ? (
                  <div className="grid min-w-0 items-start gap-4 xl:grid-cols-[minmax(280px,340px)_minmax(0,1fr)]">
                    <TariffCandidateList
                      candidates={viewModel.candidates}
                      selectedKey={selectedJobId === resultJobId ? selectedKey : ""}
                      onSelect={SelectCandidate}
                    />
                    <ClassificationEvidencePanel candidate={selectedCandidate} trace={trace} />
                  </div>
                ) : null}
                </motion.div>
              </AnimatePresence>
              <div className="md:hidden">
                <ClassificationPager activeStep={classificationStep} onSelect={PickClassificationStep} />
              </div>
            </>
          ) : null}
          {activeStage === "document_recommendation" ? (
            <DocumentRecommendationPanel
              result={result}
              viewModel={viewModel}
              selectedCandidate={selectedCandidate}
            />
          ) : null}
            </main>
          </section>
        ) : null}
      </div>
    </div>
  );
}
