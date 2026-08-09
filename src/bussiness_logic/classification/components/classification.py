"""Classification_Component — staged HS4 -> HS6 -> CN8 classifier."""
from __future__ import annotations

import logging
from copy import deepcopy

from bussiness_logic.pipeline.component_base import BasePipelineComponent
from bussiness_logic.classification.services.taric_branch_resolver import TaricBranchResolverTool
from bussiness_logic.pipeline.blackboard import BlackboardStore, now_iso
from bussiness_logic.utils.json_types import JsonObject


_LOGGER = logging.getLogger(__name__)


class ClassificationComponent(BasePipelineComponent):
    component_name = "Classification_Component"
    stage = "Classification"
    llm_model = None

    def __init__(
        self,
        *,
        validationRuntimeAdapter: object | None = None,
    ) -> None:
        super().__init__()
        self._taric_resolver = TaricBranchResolverTool()
        self._validationRuntimeAdapter = validationRuntimeAdapter
        self._stagedFailure: JsonObject = {}

    def Run(self, store: BlackboardStore) -> None:
        bb = store.load()
        pes = bb.get("product_evidence_state") or {}
        if not pes:
            raise RuntimeError("No InputEvidenceState on the Blackboard.")
        self.ReadBlackBoard(pes["product_id"])
        routingContext = bb.get("routing_context")
        if isinstance(routingContext, dict):
            routingContextId = str(
                routingContext.get("routing_decision_id")
                or routingContext.get("routing_context_id")
                or ""
            )
            if routingContextId:
                self.ReadBlackBoard(routingContextId)
        else:
            routingContext = None

        if self._maybe_classify_staged(store, pes, routingContext, bb):
            return

        failureCode = str(
            self._stagedFailure.get("failure_code")
            or "staged_classifier_unavailable"
        )
        self._emit_unresolved(
            store,
            pes,
            why=failureCode,
            debug={"failure": dict(self._stagedFailure)},
            classificationStatus="failed",
        )
        raise RuntimeError(failureCode)

    def _maybe_classify_staged(
        self,
        store: BlackboardStore,
        pes: dict,
        routingContext,
        bb: dict,
    ) -> bool:
        import os

        self._stagedFailure = {}
        # Staged narrowing is the only runtime classification path. Legacy
        # Stage1 code remains in-tree as reference code, not as fallback.
        gate = (os.environ.get("ASAP_USE_STAGED_CLASSIFIER", "1") or "").strip().lower()
        if gate in ("0", "false", "no", "off"):
            self.reason("Staged classifier disabled by env; no legacy Stage1 fallback.")
            return self._remember_staged_failure(
                "staged_classifier_disabled",
                "configuration",
            )
        try:
            product_facts = deepcopy(bb.get("product_understanding") or {})
            if not product_facts:
                self.reason("Staged classifier: no product_understanding.")
                return self._remember_staged_failure(
                    "product_understanding_missing",
                    "product_understanding",
                )
            answerFacts = bb.get("classification_answer_facts") or []
            if isinstance(answerFacts, list) and answerFacts:
                product_facts["_classification_answer_facts"] = [
                    dict(item)
                    for item in answerFacts
                    if isinstance(item, dict)
                ]
            routing = routingContext if isinstance(routingContext, dict) else (
                bb.get("routing_context") or {}
            )

            from bussiness_logic.classification.services.staged_classification import StagedClassificationTool

            # [12회차 §2-4] uncooked 결정론 보강 — LLM 무접촉·발동 3조건
            # 전부 이산(조리 어휘 0 ∧ 백과 원물 서술 ∧ 라우터 선두 원물
            # 챕터). 서명 derived_state:uncooked(...) — 2급 파생(교대
            # 응답·지지만, 단독 확정 인장은 상태 단독 게이트가 차단).
            from bussiness_logic.classification.services.derived_state import (
                DeriveUncookedState,
            )
            product_facts, _ds_sig = DeriveUncookedState(
                product_facts, routing or {})
            if _ds_sig:
                self.reason(f"Derived state (deterministic): {_ds_sig}")

            stagedTool = StagedClassificationTool(
                validationRuntimeAdapter=self._validationRuntimeAdapter,
            )
            staged = stagedTool.classify(
                product_facts=product_facts,
                routing_context=routing,
            )
            stages = staged.get("stages") or []
            candidates = staged.get("candidates") or []
            if staged.get("classification_status") == "needs_more_facts":
                pendingQuestions = [
                    dict(item)
                    for item in (staged.get("pending_user_questions") or [])
                    if (
                        isinstance(item, dict)
                        and str(item.get("contract_version") or "").isdigit()
                        and int(item["contract_version"]) > 0
                        and str(item.get("question_key") or "").strip()
                        and str(item.get("question_text") or "").strip()
                        and str(item.get("predicate_op") or "").strip()
                    )
                ]
                if not pendingQuestions:
                    return self._remember_staged_failure(
                        self._safe_failure_code(
                            staged.get("error"),
                            "question_generation_failed",
                        ),
                        str(staged.get("unresolved_stage") or "classification"),
                    )
                self._emit_unresolved(
                    store,
                    pes,
                    why=str(staged.get("error") or "needs_more_facts"),
                    userQuestions=pendingQuestions,
                    debug={
                        "resolved_level": staged.get("resolved_level") or "",
                        "resolved_prefix": staged.get("resolved_prefix") or "",
                        "unresolved_stage": staged.get("unresolved_stage") or "",
                        "unresolved": staged.get("unresolved") or {},
                        "suggestions": staged.get("suggestions") or [],
                        "classification_trace": {
                            "mode": "staged_narrowing",
                            "stages": stages,
                            "bti_summons": staged.get("bti_summons") or [],
                        },
                    },
                )
                return True
            if not staged.get("ok") or not candidates:
                failureCode = self._safe_failure_code(
                    staged.get("error"),
                    "staged_no_candidates",
                )
                self.reason(
                    "Staged classifier produced no candidates "
                    f"(error={failureCode})."
                )
                failureStage = str(staged.get("unresolved_stage") or "")
                if not failureStage and stages and isinstance(stages[-1], dict):
                    failureStage = str(
                        stages[-1].get("level")
                        or stages[-1].get("stage")
                        or ""
                    )
                return self._remember_staged_failure(
                    failureCode,
                    failureStage or "classification",
                )

            # Validator는 기록된 대안만 검토하고 결과 변경 없이 관측값을 남긴다.
            validatorRecord: dict | None = None
            verdict = stagedTool.validate_selection(
                staged=staged,
                product_facts=product_facts,
                routing_context=routing,
            )
            if verdict.get("fired"):
                validatorRecord = dict(verdict)
            scope = verdict.get("code") or verdict.get("chapter") or verdict.get("heading")
            if verdict.get("verdict") == "reroute" and scope:
                # reroute = 라우터 의견 '복원' 전용: 대상 챕터가 라우터
                # 자체 순위에서 staged 최종 챕터보다 높을 때만 허용.
                # staged가 강해진 뒤 reroute는 구제 풀이 말라 개악만 남았다
                # (최종 기준런 실측: 개악 3 — 쪽갈비 16→02, 떡볶이·산채
                # 19→21 — vs 구제 1). validator가 제3의 챕터를 발명하는
                # 것을 구조로 금지한다. ASAP_VALIDATOR_REROUTE_RESTORE_ONLY=0 복귀.
                restore_only = (os.environ.get(
                    "ASAP_VALIDATOR_REROUTE_RESTORE_ONLY", "1") or "1").strip() != "0"
                if restore_only:
                    order = [
                        str(d.get("chapter"))
                        for d in ((routing or {}).get("candidate_chapter_details") or [])
                        if isinstance(d, dict)
                    ]
                    target_ch = str(scope)[:2].zfill(2)
                    current_ch = str((candidates[0] or {}).get("cn8") or "")[:2]
                    t_rank = order.index(target_ch) if target_ch in order else 999
                    c_rank = order.index(current_ch) if current_ch in order else 999
                    if t_rank >= c_rank:
                        validatorRecord = {
                            **verdict,
                            "applied": False,
                            "blocked": "reroute_restore_only",
                        }
                        self.reason(
                            f"Validator reroute->{target_ch} blocked: not a router-"
                            f"restore (router rank {t_rank} vs current {c_rank})."
                        )
                        scope = None
            # [11회차 2-C] Validator 개입 권한 **전면** 폐지 완결
            # (CYCLE9_SPEC §6 원안·설계자 (b)안 확정). 10회차 이식이
            # promote_recovery/reroute/narrow만 폐지하고
            # promote_candidate를 남겨 불완전 집행됐던 것을 여기서
            # 종결한다 — 이제 네 판정 유형 전부가 '기록만'이다.
            # 폐지 근거(오염 실측): 22캐시 95% 헤드라인에 청양고추
            # 2001→0710·대구살 0309·기저귀 3923 교체가 우리 기전
            # 성과로 오귀속돼 측정기가 거짓말을 하고 있었다. 손실
            # 케이스는 '결정층 취약 지대 지도'로 남는다(관측 전용
            # 설계의 산출물 — would_have_* 실물 보존).
            _verdict_kind = str(verdict.get("verdict") or "")
            _would_pick = str(verdict.get("cn8") or "") if (
                _verdict_kind == "promote_candidate") else ""
            if _verdict_kind in ("promote_candidate", "promote_recovery",
                                 "reroute", "narrow") and (scope or _would_pick):
                scope = str(scope or "")
                if len(scope) == 8:
                    scope = scope[:6]
                validatorRecord = {
                    **verdict,
                    "applied": False,
                    "authority_revoked": True,
                    "revoked_kind": _verdict_kind,
                    "would_have_scope": scope,
                    "would_have_pick": _would_pick,
                    "original_top_cn8": str(
                        (candidates[0] or {}).get("cn8") or ""),
                }
                self.reason(
                    f"Validator authority revoked (observe-only): "
                    f"{_verdict_kind} -> {scope or _would_pick} recorded, "
                    f"not applied."
                )

            ccs_id = store.next_id("ccs")
            ccs_candidates: list[dict] = []
            classificationPaths = [
                path for path in (staged.get("paths") or []) if isinstance(path, dict)
            ]
            pathByCn8 = {
                str(path.get("cn8") or "")[:8]: path
                for path in classificationPaths
                if str(path.get("cn8") or "")[:8]
            }
            finalCandidates = [
                (candidate, cn8)
                for candidate in candidates[:5]
                if (
                    cn8 := str(candidate.get("cn8") or "")[:8]
                ).isdigit()
                and len(cn8) == 8
            ]
            finalCn8s = [cn8 for _candidate, cn8 in finalCandidates]
            taricBranchesByCn8 = self._resolve_taric_branches_many(finalCn8s)
            ebtiRowsByCn8 = self._load_ebti_case_pools(finalCn8s)
            for candidate, cn8 in finalCandidates:
                taric_branches = taricBranchesByCn8.get(cn8, [])
                selected_branch = self._select_taric_branch(taric_branches)
                taric10 = selected_branch.get("taric10") or ""
                rank = len(ccs_candidates) + 1
                ccs_candidates.append({
                    "candidate_id": store.next_id("cand"),
                    "hs6": cn8[:6],
                    "cn8": cn8,
                    "hs8": cn8,  # 신 external 계열과 키 호환
                    "taric10": taric10,
                    "taric10_branch_candidates": taric_branches,
                    "taric10_resolution_mode": (
                        "enumerate_all_under_cn8" if taric_branches else "no_taric_branch_found"
                    ),
                    "taric10_is_recommended": False,
                    "taric10_branch_count": len(taric_branches),
                    "rank": rank,
                    "status": "proposed",
                    "candidate_source": "staged_classifier",
                    "llm_recommended": rank == 1,
                    "candidate_static_tree": self._staged_static_tree(
                        candidate,
                        pathByCn8.get(cn8),
                    ),
                    "hard_conditions": "",
                    "hard_condition_status": "not_applicable",
                    "hard_condition_evidence": [],
                    "classification_basis": [self._staged_basis(candidate)],
                    "supporting_product_facts": [],
                    "classification_evidence_refs": [],
                    # EBTI 판례는 표시 전용(원용도) — 선택·점수·검증 어디에도
                    # 관여하지 않는다 (판례의 코드 편향을 선택에 수입 금지).
                    "similar_ebti_cases": self._ebti_cases(
                        cn8,
                        product_facts,
                        preloadedRows=ebtiRowsByCn8.get(cn8, ()),
                    ),
                    "classification_citations": list(getattr(self, "_ontology_reads", []) or []),
                    "required_facts": [],
                    "unknowns": [],
                })
                # [11회차-1] 서명 passthrough — 계약 목록(staged.SIGNATURE_
                # CONTRACT, 단일 진실원)에 등재된 키는 후보 dict에서 전부
                # 승계. 수기 화이트리스트가 서명을 탈락시키던 기록 사각
                # 3차의 구조 처방 — 신규 서명은 계약 등재만으로 통과.
                from bussiness_logic.classification.services.staged_classification import (
                    SIGNATURE_CONTRACT,
                )
                for _sig_k in SIGNATURE_CONTRACT:
                    if _sig_k in candidate:
                        ccs_candidates[-1][_sig_k] = candidate[_sig_k]

            if not ccs_candidates:
                self.reason("Staged classifier produced no valid CN8.")
                return self._remember_staged_failure(
                    "invalid_cn8_candidates",
                    "cn8",
                )
            selectedCn8 = str((ccs_candidates[0] or {}).get("cn8") or "")[:8]
            selectedPath = pathByCn8.get(selectedCn8) or (
                classificationPaths[0] if classificationPaths else {}
            )

            store.append("candidate_code_sets", {
                "object_type": "CandidateCodeSet",
                "created_by": self.component_name,
                "created_at": now_iso(),
                "candidate_set_id": ccs_id,
                "product_id": pes["product_id"],
                "classification_trace": {
                    "mode": "staged_narrowing",
                    "stages": stages,
                    "validator": validatorRecord,
                    "backtracking_recommended": bool(
                        validatorRecord and validatorRecord.get("applied")
                    ),
                },
                "classification_paths": classificationPaths,
                "recovery_candidates": staged.get("recovery_candidates") or [],
                "route_disagreements": staged.get("route_disagreements") or [],
                # [8회차-0] 개입 서명 의무화 — G1/G2·병합 게이트·BTI 소환의
                # 관측/발동 기록을 blackboard에 영속화. ON 모드가 무서명으로
                # 개입한 6런 A/B 사고(22캐시 기록 0)의 처방: staged 결과에는
                # 있었으나 이 지점이 싣지 않아 유실됐다. validator 재실행
                # (rerun) 경로도 staged 교체 후 동일 키를 읽으므로 함께 남는다.
                "router_trust_gate": staged.get("router_trust_gate") or [],
                "merge_gate_observations": staged.get("merge_gate_observations") or [],
                "bti_summons": staged.get("bti_summons") or [],
                "selected_path": selectedPath,
                "candidates": ccs_candidates,
            })
            self.WriteBlackBoard(ccs_id)
            for c in ccs_candidates:
                self.WriteBlackBoard(c["candidate_id"])
            self.reason(
                f"Staged narrowing emitted {len(ccs_candidates)} candidate(s) "
                f"(hs4->hs6->cn8, {len(stages)} stages)."
            )
            return True
        except Exception as exc:  # noqa: BLE001 — component boundary records failure
            _LOGGER.exception("Staged classification failed")
            self.reason(f"Staged classifier error ({type(exc).__name__}).")
            return self._remember_staged_failure(
                "staged_classifier_exception",
                "classification",
                exceptionType=type(exc).__name__,
            )

    @staticmethod
    def _safe_failure_code(value: object, fallback: str) -> str:
        code = str(value or "").strip().lower()
        return code if code and code.replace("_", "").isalnum() else fallback

    def _remember_staged_failure(
        self,
        code: str,
        stage: str,
        *,
        exceptionType: str = "",
    ) -> bool:
        self._stagedFailure = {
            "failure_code": self._safe_failure_code(
                code,
                "staged_classifier_unavailable",
            ),
            "failure_stage": str(stage or "classification"),
        }
        if exceptionType:
            self._stagedFailure["exception_type"] = exceptionType
        return False

    def _staged_static_tree(
        self,
        candidate: dict,
        classificationPath: dict | None = None,
    ) -> dict:
        levelScoresValue = (
            classificationPath.get("level_scores")
            if isinstance(classificationPath, dict)
            else {}
        )
        levelScores = levelScoresValue if isinstance(levelScoresValue, dict) else {}
        candidateScore = self._read_optional_float(candidate.get("score"))
        cn8Score = self._read_optional_float(levelScores.get("cn8"))
        totalScore = candidateScore if candidateScore is not None else cn8Score
        nodes: list[dict] = []
        for level, label in (("hs4", "HS4"), ("hs6", "HS6"), ("cn8", "CN8")):
            code = str(candidate.get(level) or "").strip()
            if not code:
                continue
            description = (
                classificationPath.get(f"{level}_description")
                if isinstance(classificationPath, dict)
                else ""
            )
            node = {
                "level": level,
                "label": label,
                "code": code,
                "description": str(
                    description
                    or (candidate.get("description") if level == "cn8" else "")
                    or ""
                ),
                "matched_keywords": [],
            }
            levelScore = self._read_optional_float(levelScores.get(level))
            if levelScore is None and level == "cn8":
                levelScore = candidateScore
            if levelScore is not None:
                node["score"] = levelScore
            nodes.append(node)
        staticTree = {
            "level_scores": {
                level: score
                for level in ("hs4", "hs6", "cn8")
                if (score := self._read_optional_float(levelScores.get(level)))
                is not None
            },
            "retrieval_sources": ["staged_narrowing"],
            "nodes": nodes,
        }
        if totalScore is not None:
            staticTree["total_score"] = totalScore
        return staticTree

    @staticmethod
    def _read_optional_float(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ebti_cases(
        cn8: str,
        product_facts: dict,
        *,
        preloadedRows=None,
    ) -> list[dict]:
        """유사 EBTI 판례 (표시 전용) — 실패는 빈 목록, 파이프라인 무영향."""
        try:
            from bussiness_logic.classification.services.ebti_precedent_local import FindSimilarCases

            ih = product_facts.get("identity_hints") or {}
            identity_text = " ".join(
                str(part) for part in (
                    ih.get("normalized_tariff_description") or "",
                    ih.get("translated_product_name") or "",
                    " ".join(ih.get("identity_terms") or []),
                    ih.get("ingredient_class") or "",
                ) if part
            )
            return FindSimilarCases(
                cn8,
                identity_text,
                limit=2,
                preloaded_rows=preloadedRows,
            )
        except Exception:  # noqa: BLE001 — 근거 표시 실패가 분류를 깨면 안 된다
            return []

    @staticmethod
    def _load_ebti_case_pools(cn8s: list[str]) -> dict[str, tuple[dict, ...]]:
        if not cn8s:
            return {}
        try:
            from bussiness_logic.core.runtime_asset_repository import (
                LoadBtiCasesForCodes,
            )

            return LoadBtiCasesForCodes(cn8s)
        except Exception:  # noqa: BLE001 — 표시 근거 실패는 분류 결과와 무관
            return {cn8: () for cn8 in cn8s}

    def _staged_basis(self, candidate: dict) -> str:
        desc = str(candidate.get("description") or "")[:160]
        verdict = str(candidate.get("quantitative_verdict") or "")
        base = f"Staged narrowing hs4->hs6->cn8 selected CN8={candidate.get('cn8')}: {desc}"
        if verdict and verdict != "neutral":
            base += f" [%-gate: {verdict}]"
        return base[:600]


    def _resolve_taric_branches(self, cn8: str) -> list[JsonObject]:
        if not cn8 or cn8 == "99999999":
            return []
        try:
            branches = self._taric_resolver.resolve(
                cn8,
                only_declarable_leaf=False,
                only_kr_applicable=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.reason(f"TaricBranchResolverTool error for CN8={cn8}: {exc}")
            return []

        out = [b.to_dict() for b in branches]
        if out:
            self.CreateCiteSource(
                "taric_master_table",
                f"cn8={cn8}",
                snippet=f"{len(out)} TARIC10 branch candidate(s)",
                reason="TaricBranchResolverTool branch retrieval.",
            )
        return out

    def _resolve_taric_branches_many(
        self,
        cn8s: list[str],
    ) -> dict[str, list[JsonObject]]:
        if not cn8s:
            return {}
        try:
            resolved = self._taric_resolver.resolve_many(
                cn8s,
                only_declarable_leaf=False,
                only_kr_applicable=False,
            )
        except Exception as exc:  # noqa: BLE001
            self.reason(f"TaricBranchResolverTool batch error: {exc}")
            return {cn8: self._resolve_taric_branches(cn8) for cn8 in cn8s}

        out: dict[str, list[JsonObject]] = {}
        for cn8 in cn8s:
            branches = [branch.to_dict() for branch in resolved.get(cn8, [])]
            out[cn8] = branches
            if branches:
                self.CreateCiteSource(
                    "taric_master_table",
                    f"cn8={cn8}",
                    snippet=f"{len(branches)} TARIC10 branch candidate(s)",
                    reason="TaricBranchResolverTool batched branch retrieval.",
                )
        return out

    # ------------------------------------------------------------------ helpers
    def _select_taric_branch(self, branches: list[JsonObject]) -> JsonObject:
        """Pick a compatibility primary TARIC10 from deterministic branches.

        This is not a legal recommendation. The full branch list remains on
        the candidate and Document_Component packages every branch. The primary
        value only preserves older UI/API paths that expect cand["taric10"].
        """
        if not branches:
            return {}

        def score(branch: JsonObject) -> tuple[int, int, int, int, int]:
            description = (branch.get("branch_description") or "").strip().lower()
            return (
                1 if branch.get("applies_to_origin_kr") else 0,
                1 if branch.get("is_declarable_leaf") else 0,
                0 if branch.get("needs_review") else 1,
                int(branch.get("measure_row_count") or 0),
                1 if description == "other" else 0,
            )

        selected = max(branches, key=score)
        out = dict(selected)
        out["selection_reason"] = (
            "Compatibility primary only, not a TARIC10 recommendation. "
            "All TARIC master branches under this CN8 are retained in "
            "taric10_branch_candidates; this primary prefers KR-applicable "
            "declarable leaves, then non-review branches, then measure coverage."
        )
        return out
    def _emit_unresolved(
        self,
        store: BlackboardStore,
        pes: dict,
        *,
        why: str,
        debug: JsonObject | None = None,
        userQuestions: list[dict] | None = None,
        classificationStatus: str = "needs_more_facts",
    ) -> None:
        ccs_id = store.next_id("ccs")
        candidateCodeSet = {
            "object_type": "ClassificationCandidateSet",
            "created_by": self.component_name,
            "created_at": now_iso(),
            "candidate_set_id": ccs_id,
            "product_id": pes["product_id"],
            "classification_status": classificationStatus,
            "failure_reason": why,
            "shortlisted_candidates": list(self._ontology_reads),
            "candidates": [],
        }
        if debug:
            candidateCodeSet["resolver_debug"] = debug
        store.append("candidate_code_sets", candidateCodeSet)
        self.WriteBlackBoard(ccs_id)
        existingQuestions = [
            item
            for item in (store.load().get("user_questions") or [])
            if isinstance(item, dict)
        ]
        existingStableKeys = {
            str(item.get("question_key") or "")
            for item in existingQuestions
            if str(item.get("question_key") or "")
        }
        seenQuestions: set[tuple[str, tuple[str, ...]]] = {
            (
                str(item.get("question_text") or "").strip(),
                tuple(str(value) for value in (item.get("required_for") or [])),
            )
            for item in existingQuestions
        }
        for pendingQuestion in userQuestions or []:
            questionText = str(pendingQuestion.get("question_text") or "").strip()
            stableKey = str(pendingQuestion.get("question_key") or "").strip()
            requiredFor = tuple(
                str(item)
                for item in (pendingQuestion.get("required_for") or [])
                if str(item)
            )
            questionKey = (questionText, requiredFor)
            if (
                not questionText
                or (stableKey and stableKey in existingStableKeys)
                or questionKey in seenQuestions
            ):
                continue
            seenQuestions.add(questionKey)
            if stableKey:
                existingStableKeys.add(stableKey)
            questionId = store.next_id("uq")
            store.append("user_questions", {
                "object_type": "UserQuestion",
                "created_by": self.component_name,
                "created_at": now_iso(),
                "user_question_id": questionId,
                "contract_version": int(pendingQuestion.get("contract_version") or 0),
                "question_key": stableKey,
                "question_text": questionText,
                "asked_by": self.component_name,
                "stage": str(pendingQuestion.get("stage") or ""),
                "parent_code": str(pendingQuestion.get("parent_code") or ""),
                "candidate_code": str(pendingQuestion.get("candidate_code") or ""),
                "axis": str(pendingQuestion.get("axis") or ""),
                "predicate_op": str(pendingQuestion.get("predicate_op") or ""),
                "canonical_field": str(pendingQuestion.get("canonical_field") or ""),
                "condition_value": pendingQuestion.get("condition_value"),
                "context_scope": str(pendingQuestion.get("context_scope") or ""),
                "bti_evidence": [
                    dict(item)
                    for item in (pendingQuestion.get("bti_evidence") or [])
                    if isinstance(item, dict)
                ],
                "active": True,
                "resolved_at": None,
                "required_for": list(requiredFor),
                "options": [
                    str(option)
                    for option in (pendingQuestion.get("options") or [])
                    if str(option)
                ],
                "answer": None,
                "answered_at": None,
            })
            self.WriteBlackBoard(questionId)
        self.reason(
            f"Classification unresolved ({why}); wrote empty ClassificationCandidateSet "
            "instead of a synthetic 99999999 candidate."
        )
