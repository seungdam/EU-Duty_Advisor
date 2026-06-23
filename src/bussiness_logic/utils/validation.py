"""외부 dict payload 검증에 사용하는 작은 helper 함수."""

from typing import Any, Dict, List, Set, Annotated



def ReadRequiredString(
    data: Dict[str, Any],
    fieldName: str,
    errors: List[str],
) -> str:
    value = data.get(fieldName)
    if not isinstance(value, str):
        errors.append("{0} must be a string".format(fieldName))
        return ""

    return value.strip()

def ReadRequiredBool(
    data: Dict[str, Any],
    fieldName: str,
    errors: List[str],
) -> bool:
    value = data.get(fieldName)
    if not isinstance(value, bool):
        errors.append("{0} must be a boolean".format(fieldName))
        return False

    return value


def ReadStringList(
    data: Dict[str, Any],
    fieldName: str,
    errors: List[str],
) -> List[str]:
    value = data.get(fieldName)
    if not isinstance(value, list):
        errors.append("{0} must be a list".format(fieldName))
        return []

    stringValues: List[str] = []
    for item in value:
        if not isinstance(item, str):
            errors.append("{0} must contain only strings".format(fieldName))
            return []

        normalizedItem = item.strip()
        if normalizedItem != "":
            stringValues.append(normalizedItem)

    return stringValues


def ReadOptionalStringList(
    data: Dict[str, Any],
    fieldName: str,
    errors: List[str],
) -> List[str]:
    if fieldName not in data:
        return []

    return ReadStringList(data, fieldName, errors)


def ReadNumberInRange(
    data: Dict[str, Any],
    fieldName: str,
    minValue: float,
    maxValue: float,
    errors: List[str],
) -> float:
    value = data.get(fieldName)
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        errors.append("{0} must be a number".format(fieldName))
        return 0.0

    numberValue = float(value)
    if numberValue < minValue or numberValue > maxValue:
        errors.append(
            "{0} must be between {1} and {2}".format(
                fieldName,
                minValue,
                maxValue,
            )
        )
        return 0.0

    return numberValue
