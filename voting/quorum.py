"""Preset and integer calculations for verified assembly deliberations."""

import re
from dataclasses import asdict, dataclass

from django.core.exceptions import ValidationError

from .models import (
    AssemblyKind,
    ConstitutiveQuorum,
    Convocation,
    DeliberationKind,
    DeliberativeMajority,
    Question,
    QuorumBasis,
    VotingQuorum,
)

CONFIGURATION_VERSION = 1
EXTRAORDINARY_MATTERS = {
    DeliberationKind.STATUTE,
    DeliberationKind.LEGAL_PERSON,
    DeliberationKind.REORGANIZATION,
    DeliberationKind.DISSOLUTION,
}


DELIBERATION_PRESETS = {
    DeliberationKind.ORDINARY: (DeliberativeMajority.VOTES_CAST, "", ""),
    DeliberationKind.STATUTE: (DeliberativeMajority.PRESENT_ABSOLUTE, "", ""),
    DeliberationKind.LEGAL_PERSON: (
        DeliberativeMajority.QUALIFIED,
        "2/3",
        QuorumBasis.PARTICIPANTS,
    ),
    DeliberationKind.REORGANIZATION: (
        DeliberativeMajority.QUALIFIED,
        "2/3",
        QuorumBasis.PARTICIPANTS,
    ),
    DeliberationKind.DISSOLUTION: (
        DeliberativeMajority.QUALIFIED,
        "3/4",
        QuorumBasis.MEMBERS,
    ),
}


@dataclass(frozen=True)
class Rule:
    basis: str
    numerator: int
    denominator: int
    strict: bool = False
    fixed: int | None = None

    def minimum(self, size):
        if self.fixed is not None:
            return self.fixed
        scaled = size * self.numerator
        if self.strict:
            return scaled // self.denominator + 1
        return (scaled + self.denominator - 1) // self.denominator

    @property
    def label(self):
        if self.fixed is not None:
            return f"Almeno {self.fixed} votanti"
        if not self.numerator:
            return "Libero"
        comparison = "Più di" if self.strict else "Almeno"
        return f"{comparison} {self.numerator}/{self.denominator} · {QuorumBasis(self.basis).label.lower()}"


def parse_rule(value, basis):
    if value == "libero" and basis == QuorumBasis.ELIGIBLE:
        return Rule(basis, 0, 1)
    match = re.fullmatch(r"(>)?([1-9]\d{0,5})/([1-9]\d{0,5})", value)
    if not match:
        raise ValidationError(
            "Inserisci una frazione come 2/3 oppure >1/2 per una soglia stretta."
        )
    strict, numerator, denominator = match.groups()
    numerator, denominator = int(numerator), int(denominator)
    if numerator > denominator or (strict and numerator == denominator):
        raise ValidationError(
            "La soglia deve essere raggiungibile con il totale del denominatore."
        )
    return Rule(basis, numerator, denominator, bool(strict))


def at_least_as_strict_as(chosen, preset):
    left = chosen.numerator * preset.denominator
    right = preset.numerator * chosen.denominator
    bases = [
        QuorumBasis.EXPRESSED,
        QuorumBasis.PARTICIPANTS,
        QuorumBasis.ELIGIBLE,
        QuorumBasis.MEMBERS,
    ]
    return bases.index(chosen.basis) >= bases.index(preset.basis) and (
        left > right or (left == right and (chosen.strict or not preset.strict))
    )


def constitution_rule(
    assembly_type, convocation, override="", *, dissolution_only=False
):
    if convocation == Convocation.FIRST:
        preset = Rule(QuorumBasis.ELIGIBLE, 1, 2, strict=True)
    elif assembly_type == AssemblyKind.EXTRAORDINARY:
        preset = Rule(QuorumBasis.ELIGIBLE, 1, 3)
    else:
        preset = Rule(QuorumBasis.ELIGIBLE, 0, 1)
    if not override:
        return preset
    chosen = parse_rule(override, QuorumBasis.ELIGIBLE)
    if not dissolution_only and not at_least_as_strict_as(chosen, preset):
        raise ValidationError(
            "La soglia costitutiva personalizzata deve rispettare il minimo del preset."
        )
    return chosen


def question_constitution_rule(mode, fraction="", minimum=None):
    if mode == ConstitutiveQuorum.FIXED:
        if minimum is None or minimum < 1:
            raise ValidationError(
                "Indica un numero minimo di votanti maggiore di zero."
            )
        return Rule(QuorumBasis.ELIGIBLE, 0, 1, fixed=minimum)
    if mode == ConstitutiveQuorum.FRACTION:
        return parse_rule(fraction, QuorumBasis.ELIGIBLE)
    if mode == ConstitutiveQuorum.ABSOLUTE:
        return Rule(QuorumBasis.ELIGIBLE, 1, 2, strict=True)
    if mode == ConstitutiveQuorum.FREE:
        return Rule(QuorumBasis.ELIGIBLE, 0, 1)
    if mode:
        raise ValidationError("Seleziona una soglia valida per il quesito.")
    return None


def majority_rule(mode, fraction="", basis=""):
    if mode == DeliberativeMajority.VOTES_CAST:
        return Rule(QuorumBasis.EXPRESSED, 1, 2, strict=True)
    if mode == DeliberativeMajority.PRESENT_ABSOLUTE:
        return Rule(QuorumBasis.PARTICIPANTS, 1, 2, strict=True)
    if mode == DeliberativeMajority.QUALIFIED:
        if not fraction or basis not in QuorumBasis.values:
            raise ValidationError(
                "Indica la frazione e il denominatore della maggioranza qualificata."
            )
        rule = parse_rule(fraction, basis)
        if rule.strict or not rule.numerator:
            raise ValidationError(
                "Indica una frazione come 2/3. La soglia si arrotonda all’intero superiore."
            )
        return rule
    raise ValidationError("Seleziona un criterio deliberativo.")


def deliberation_rule(kind, mode="", fraction="", basis=""):
    if kind == DeliberationKind.ADVISORY:
        if mode:
            raise ValidationError(
                "Una consultazione non deliberativa usa solo i conteggi."
            )
        return "", None
    preset = DELIBERATION_PRESETS.get(kind)
    if not mode and preset:
        return preset[0], majority_rule(*preset)
    chosen = majority_rule(mode, fraction, basis)
    if preset and not at_least_as_strict_as(chosen, majority_rule(*preset)):
        raise ValidationError(
            "La maggioranza indicata è inferiore alla soglia del preset."
        )
    return mode, chosen


def question_quorum_outcome(rule, eligible, answered):
    if rule is None:
        return None
    minimum = rule.minimum(eligible)
    return {
        "label": rule.label,
        "minimum": minimum,
        "counted": answered,
        "met": answered >= minimum,
    }


def constitution_outcome(rule, eligible, recognized):
    minimum = rule.minimum(eligible)
    return {
        "label": rule.label,
        "minimum": minimum,
        "counted": recognized,
        "met": recognized > 0 and recognized >= minimum,
    }


def deliberation_outcome(
    rule, mode, constitution_met, *, eligible, members, participants, favorable, against
):
    if rule is None:
        return {
            "label": "Consultazione non deliberativa",
            "minimum": None,
            "denominator": None,
            "approved": None,
        }
    denominator = {
        QuorumBasis.ELIGIBLE: eligible,
        QuorumBasis.EXPRESSED: favorable + against,
        QuorumBasis.PARTICIPANTS: participants,
        QuorumBasis.MEMBERS: members,
    }[rule.basis]
    minimum = rule.minimum(denominator)
    approved = constitution_met and favorable > 0 and favorable >= minimum
    return {
        "criterion": DeliberativeMajority(mode).label,
        "label": rule.label,
        "minimum": minimum,
        "denominator": denominator,
        "approved": approved,
    }


def validate_minimum_voters(election, eligible):
    if election.questions.filter(minimum_voters__gt=eligible).exists():
        raise ValidationError(
            "Il numero minimo di votanti di un quesito supera gli aventi diritto."
        )
    if election.questions.filter(majority_basis=QuorumBasis.MEMBERS).exists() and (
        not election.total_members or election.total_members < eligible
    ):
        raise ValidationError(
            "Indica un totale degli associati pari o superiore agli aventi diritto."
        )


def configuration(election, eligible):
    questions = list(election.questions.all())
    if not election.total_members or election.total_members < eligible:
        raise ValidationError(
            "Indica un totale degli associati pari o superiore agli aventi diritto."
        )
    kinds = {question.deliberation_type for question in questions}
    if (
        kinds & EXTRAORDINARY_MATTERS
        and election.assembly_type != AssemblyKind.EXTRAORDINARY
    ):
        raise ValidationError(
            "Per i quesiti straordinari scegli un’assemblea straordinaria."
        )
    if election.constitutive_override and not election.allow_custom_quorum:
        raise ValidationError(
            "Autorizza i quorum personalizzati prima di impostare una soglia costitutiva."
        )
    dissolution_only = DeliberationKind.DISSOLUTION in kinds and kinds <= {
        DeliberationKind.DISSOLUTION,
        DeliberationKind.ADVISORY,
    }
    constitutional = constitution_rule(
        election.assembly_type,
        election.convocation,
        election.constitutive_override,
        dissolution_only=dissolution_only,
    )
    agenda = {}
    for question in questions:
        motion = question.kind in {Question.Kind.MOTION, Question.Kind.MOTION_REQUIRED}
        kind = question.deliberation_type or (
            DeliberationKind.ORDINARY if motion else DeliberationKind.ADVISORY
        )
        if (
            kind == DeliberationKind.CUSTOM or question.deliberative_majority
        ) and not election.allow_custom_quorum:
            raise ValidationError(
                "Autorizza i quorum personalizzati nella configurazione dell’assemblea."
            )
        mode, rule = deliberation_rule(
            kind,
            question.deliberative_majority,
            question.majority_override,
            question.majority_basis,
        )
        question_rule = question_constitution_rule(
            question.constitutive_quorum,
            question.constitutive_fraction,
            question.minimum_voters,
        )
        if question_rule is None and question.voting_quorum in VotingQuorum.values:
            fraction = (
                "libero"
                if question.voting_quorum == VotingQuorum.FREE
                else question.voting_quorum
            )
            question_rule = parse_rule(fraction, QuorumBasis.ELIGIBLE)
        if rule is not None and not motion:
            raise ValidationError(
                f"Il quesito «{question.text}» richiede Favorevole, Contrario e Astenuto."
            )
        agenda[str(question.pk)] = {
            "question": question.text,
            "type": kind,
            "type_label": DeliberationKind(kind).label,
            "mode": mode,
            "rule": asdict(rule) if rule is not None else None,
            "rule_label": rule.label if rule is not None else "Solo conteggi",
            "question_quorum": asdict(question_rule)
            if question_rule is not None
            else None,
            "question_quorum_label": question_rule.label
            if question_rule is not None
            else "Usa il quorum dell’assemblea",
        }
    return {
        "version": CONFIGURATION_VERSION,
        "type": election.assembly_type,
        "type_label": election.get_assembly_type_display(),
        "convocation": election.convocation,
        "convocation_label": election.get_convocation_display(),
        "eligible": eligible,
        "members": election.total_members,
        "constitution": asdict(constitutional),
        "constitution_label": constitutional.label,
        "agenda": agenda,
    }


def verified_result(snapshot, participation, tally, revision):
    recognized = participation["recognized"]
    eligible, members = snapshot["eligible"], snapshot["members"]
    constitution = constitution_outcome(
        Rule(**snapshot["constitution"]), eligible, recognized
    )
    rows = []
    for row in tally["rows"]:
        key = str(row["id"])
        matter = snapshot["agenda"][key]
        participants = participation["by_question"][key]
        counts = dict(row["choices"])
        rule = Rule(**matter["rule"]) if matter["rule"] is not None else None
        question_rule = matter["question_quorum"]
        question_quorum = question_quorum_outcome(
            Rule(**question_rule) if question_rule is not None else None,
            eligible,
            row["answered"],
        )
        question_met = question_quorum is None or question_quorum["met"]
        outcome = deliberation_outcome(
            rule,
            matter["mode"],
            constitution["met"] and question_met,
            eligible=eligible,
            members=members,
            participants=participants,
            favorable=counts.get("Favorevole", 0),
            against=counts.get("Contrario", 0),
        )
        if not constitution["met"]:
            style, label = "unavailable", "Assemblea non validamente costituita"
        elif not question_met:
            style, label = "unavailable", "Quorum del quesito non raggiunto"
        elif rule is None:
            style, label = "", "Consultazione non deliberativa"
        elif outcome["approved"]:
            style, label = "approved", "Deliberazione approvata"
        else:
            style, label = "rejected", "Deliberazione non approvata"
        rows.append(
            {
                **row,
                "matter": matter["type_label"],
                "participants": participants,
                "favorable": counts.get("Favorevole", 0),
                "against": counts.get("Contrario", 0),
                "abstained": counts.get("Astenuto", 0),
                "no_choice": participants - row["answered"],
                "quorum": question_quorum,
                "deliberative": outcome,
                "outcome": style,
                "outcome_label": label,
            }
        )
    return {
        **tally,
        "eligible": eligible,
        "rows": rows,
        "quorum": constitution,
        "quorum_failed": not constitution["met"],
        "assembly": {
            "version": snapshot["version"],
            "revision": revision,
            "members": members,
            "type": snapshot["type_label"],
            "convocation": snapshot["convocation_label"],
            "recognized": recognized,
            "direct": len(participation["present"]),
            "represented": participation["represented_only"],
            "absent": eligible - recognized,
        },
    }
