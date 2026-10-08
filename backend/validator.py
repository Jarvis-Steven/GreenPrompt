import re

def check_answer(prompt: str, answer: str) -> dict:
    if not answer.strip():
        return {
            "status": "failed",
            "reason": "The model returned an empty answer."
        }

    question = prompt.strip().lower()

    match = re.fullmatch(
        r"(?:what is\s+)?(\d+)\s*([+*-])\s*(\d+)\s*\??",
        question
    )

    if match:
        first_number = int(match.group(1))
        operator = match.group(2)
        second_number = int(match.group(3))

        if operator == "+":
            expected_answer = first_number + second_number
        elif operator == "-":
            expected_answer = first_number - second_number
        else:
            expected_answer = first_number * second_number
            
        try:
            actual_answer = int(answer.strip())
        except ValueError:
            return {
                "status": "unchecked",
                "reason": "This addition check requires a whole-number answer."
            }

        if actual_answer == expected_answer:
            return {
                "status": "passed",
                "reason": "The addition result is correct."
            }

        return {
            "status": "failed",
            "reason": "The addition result is incorrect."
        }

    return {
        "status": "unchecked",
        "reason": "No correctness check is available for this answer yet."
    }