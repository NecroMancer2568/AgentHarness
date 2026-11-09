from src.miniapp import unique_preserving_order


def test_duplicates_are_removed_without_reordering():
    assert unique_preserving_order([3, 1, 3, 2, 1, 4]) == [3, 1, 2, 4]
    assert unique_preserving_order([]) == []
