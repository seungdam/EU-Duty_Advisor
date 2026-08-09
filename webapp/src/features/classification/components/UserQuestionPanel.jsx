import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { ArrowLeft, ArrowRight, Check, CircleHelp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PipelineFailureMessage } from "@/features/classification/model/classificationViewModel.js";

export default function UserQuestionPanel({
  questions,
  onSubmit,
  submitting = false,
  error = "",
}) {
  const reduceMotion = useReducedMotion();
  const [answers, setAnswers] = useState({});
  const [currentIndex, setCurrentIndex] = useState(0);
  const [direction, setDirection] = useState(1);
  const questionKey = questions.map((question) => question.user_question_id).join("|");
  const safeIndex = Math.min(currentIndex, Math.max(questions.length - 1, 0));
  const activeQuestion = questions[safeIndex];
  const selected = questions
    .filter((question) => answers[question.user_question_id])
    .map((question) => ({
      user_question_id: question.user_question_id,
      answer: answers[question.user_question_id],
    }));

  useEffect(() => {
    setAnswers((current) => {
      const next = {};
      questions.forEach((question) => {
        const answer = current[question.user_question_id] || question.answer;
        if (["yes", "no", "unknown"].includes(answer)) {
          next[question.user_question_id] = answer;
        }
      });
      return next;
    });
    const firstUnanswered = questions.findIndex(
      (question) => !["yes", "no", "unknown"].includes(question.answer),
    );
    setCurrentIndex(firstUnanswered >= 0 ? firstUnanswered : 0);
    setDirection(1);
  }, [questionKey]);

  if (!activeQuestion) return null;

  const MoveTo = (nextIndex) => {
    const boundedIndex = Math.max(0, Math.min(nextIndex, questions.length - 1));
    if (boundedIndex === safeIndex) return;
    setDirection(boundedIndex > safeIndex ? 1 : -1);
    setCurrentIndex(boundedIndex);
  };
  const activeQuestionId = activeQuestion.user_question_id;
  const selectedAnswer = answers[activeQuestionId];
  const SelectAnswer = (answer) => {
    setAnswers((current) => ({
      ...current,
      [activeQuestionId]: answer,
    }));
    const nextUnansweredIndex = questions.findIndex(
      (question, index) => index > safeIndex
        && !answers[question.user_question_id],
    );
    if (nextUnansweredIndex >= 0) {
      setDirection(1);
      setCurrentIndex(nextUnansweredIndex);
    }
  };

  return (
    <Card className="overflow-hidden border-warning/45 bg-warning/5">
      <CardHeader className="border-b bg-surface">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <CircleHelp className="size-5 text-warning" aria-hidden="true" />
            <CardTitle>분류 조건 확인</CardTitle>
          </div>
          <Badge variant="outline">답변 {selected.length}/{questions.length}</Badge>
        </div>
        <CardDescription>
          분류가 멈춘 조건만 순서대로 확인합니다. 답변은 한 번에 반영해 중단 지점부터 이어서 실행합니다.
        </CardDescription>
      </CardHeader>

      <CardContent className="grid gap-5 p-5 sm:p-6">
        <ol className="flex gap-2 overflow-x-auto pb-1" aria-label="분류 질문 목록">
          {questions.map((question, index) => {
            const answered = Boolean(answers[question.user_question_id]);
            const active = index === safeIndex;
            return (
              <li key={question.user_question_id}>
                <button
                  type="button"
                  className={`grid size-9 place-items-center rounded-full border text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${active ? "border-primary bg-primary text-primary-foreground" : answered ? "border-success/50 bg-success/10 text-success" : "border-border bg-surface text-muted-foreground hover:border-primary/40"}`}
                  aria-current={active ? "step" : undefined}
                  aria-label={`질문 ${index + 1}${answered ? ", 답변 완료" : ""}`}
                  onClick={() => MoveTo(index)}
                >
                  {answered && !active ? <Check className="size-4" aria-hidden="true" /> : index + 1}
                </button>
              </li>
            );
          })}
        </ol>

        <div
          className="relative min-h-[250px] overflow-hidden rounded-lg bg-surface-muted"
          role="region"
          aria-roledescription="carousel"
          aria-label="분류 확인 질문"
        >
          <AnimatePresence initial={false} mode="wait" custom={direction}>
            <motion.fieldset
              key={activeQuestionId}
              custom={direction}
              initial={reduceMotion ? { opacity: 0 } : { opacity: 0, x: direction > 0 ? 28 : -28 }}
              animate={{ opacity: 1, x: 0 }}
              exit={reduceMotion ? { opacity: 0 } : { opacity: 0, x: direction > 0 ? -28 : 28 }}
              transition={{ duration: reduceMotion ? 0 : 0.2, ease: [0.22, 1, 0.36, 1] }}
              className="absolute inset-0 grid content-between gap-6 p-5 sm:p-7"
            >
              <legend className="sr-only">분류 질문 {safeIndex + 1}</legend>
              <div className="grid gap-4">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <strong className="text-sm font-semibold text-primary">Q. {safeIndex + 1}</strong>
                  <Badge variant="secondary">
                    {String(activeQuestion.stage || "분류").toUpperCase()}
                  </Badge>
                  {activeQuestion.candidate_code
                    ? <span>검토 코드 {activeQuestion.candidate_code}</span>
                    : null}
                </div>
                <p className="m-0 text-lg font-semibold leading-8 text-foreground">
                  {activeQuestion.question_text}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-3" role="group" aria-label={`질문 ${safeIndex + 1} 답변`}>
                <Button
                  type="button"
                  className="min-h-11"
                  variant={selectedAnswer === "yes" ? "default" : "outline"}
                  aria-pressed={selectedAnswer === "yes"}
                  disabled={submitting}
                  onClick={() => SelectAnswer("yes")}
                >
                  예
                </Button>
                <Button
                  type="button"
                  className="min-h-11"
                  variant={selectedAnswer === "no" ? "default" : "outline"}
                  aria-pressed={selectedAnswer === "no"}
                  disabled={submitting}
                  onClick={() => SelectAnswer("no")}
                >
                  아니오
                </Button>
              </div>
            </motion.fieldset>
          </AnimatePresence>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-2">
            <Button
              type="button"
              className="w-24"
              variant="outline"
              disabled={submitting || safeIndex === 0}
              onClick={() => MoveTo(safeIndex - 1)}
            >
              <ArrowLeft className="size-4" aria-hidden="true" />
              이전
            </Button>
            <Button
              type="button"
              className="w-24"
              variant="outline"
              disabled={submitting || safeIndex === questions.length - 1}
              onClick={() => MoveTo(safeIndex + 1)}
            >
              다음
              <ArrowRight className="size-4" aria-hidden="true" />
            </Button>
          </div>
          <Button
            type="button"
            disabled={submitting || selected.length !== questions.length}
            onClick={() => onSubmit(selected)}
          >
            {submitting
              ? "분류 재개 중"
              : selected.length === questions.length
                ? "답변을 반영하고 계속"
                : `${questions.length - selected.length}개 질문 답변 필요`}
          </Button>
        </div>
        {error ? (
          <p className="m-0 text-sm text-destructive" role="alert">
            {PipelineFailureMessage(error)}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}
