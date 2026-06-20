def normalize_lineup(
    lineup: list[dict],
    expected_car_nos: set[int] | None = None,
) -> list[dict]:
    if not lineup:
        return []
    normalized = []
    for item in lineup:
        try:
            car_no = int(item["car_no"])
            line_no = int(item["line_no"])
            line_position = int(item["line_position"])
        except (KeyError, TypeError, ValueError):
            return []
        if not 1 <= car_no <= 9 or line_no < 1 or line_position < 1:
            return []
        normalized.append(
            {
                "car_no": car_no,
                "line_no": line_no,
                "line_position": line_position,
            }
        )

    car_nos = [item["car_no"] for item in normalized]
    if len(car_nos) > 9 or len(car_nos) != len(set(car_nos)):
        return []
    if expected_car_nos is not None and set(car_nos) != expected_car_nos:
        return []

    groups = {}
    for item in normalized:
        groups.setdefault(item["line_no"], []).append(item["line_position"])
    if sorted(groups) != list(range(1, len(groups) + 1)):
        return []
    if any(
        sorted(positions) != list(range(1, len(positions) + 1))
        for positions in groups.values()
    ):
        return []
    return sorted(normalized, key=lambda item: (item["line_no"], item["line_position"]))


def lineup_groups(lineup: list[dict]) -> list[list[dict]]:
    groups = {}
    for item in lineup:
        groups.setdefault(item["line_no"], []).append(item)
    return [groups[line_no] for line_no in sorted(groups)]
