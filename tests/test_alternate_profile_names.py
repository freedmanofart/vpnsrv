from types import SimpleNamespace

from app.api.routes.client import (
    alternate_profile_name,
    deduplicate_profiles,
    incy_profile_allowed,
    localized_profile_name,
    profile_identity,
    profile_name_for_node,
)


def test_alternate_profile_name_uses_configured_cascade_label():
    node = SimpleNamespace(region="RU|Россия")
    config = SimpleNamespace(
        config={
            "alternate_profile_labels": {
                "Reality": "🇺🇸 США (Reality)",
                "Reality 2": "🇵🇱 Польша (Reality)",
                "US-XHTTP": "🇺🇸 США (xHTTP)",
                "US-GRPC": "🇺🇸 США (gRPC)",
                "FI-REALITY": "🇫🇮 Финляндия (Reality)",
                "CH-REALITY": "🇨🇭 Швейцария",
            }
        }
    )

    name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?security=reality#REALITY",
        0,
        "Reality",
        443,
    )

    assert name == "🇺🇸 США (Reality)"

    poland_name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?security=reality#REALITY-2",
        1,
        "Reality 2",
        443,
    )

    assert poland_name == "🇵🇱 Польша (Reality)"

    xhttp_name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?type=xhttp&security=reality#US-XHTTP-vpn-137",
        2,
        "xHTTP",
        443,
        "US-XHTTP-vpn-137",
    )
    assert xhttp_name == "🇺🇸 США (xHTTP)"

    grpc_name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?type=grpc&security=reality#US-gRPC-vpn-137",
        3,
        "gRPC",
        443,
        "US-gRPC-vpn-137",
    )
    assert grpc_name == "🇺🇸 США (gRPC)"

    finland_name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?security=reality#FI-REALITY-vpn-137",
        4,
        "Reality",
        443,
        "FI-REALITY-vpn-137",
    )
    assert finland_name == "🇫🇮 Финляндия (Reality)"

    switzerland_name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?security=reality#CH-REALITY-vpn-137",
        5,
        "Reality",
        443,
        "CH-REALITY-vpn-137",
    )
    assert switzerland_name == "🇨🇭 Швейцария (Reality)"


def test_configured_country_only_label_keeps_actual_transport():
    node = SimpleNamespace(region="RU|Россия")
    config = SimpleNamespace(
        config={"alternate_profile_labels": {"US-XHTTP": "🇺🇸 США"}}
    )

    name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:443?type=xhttp&security=reality#US-XHTTP-vpn-137",
        0,
        "xHTTP",
        443,
        "US-XHTTP-vpn-137",
    )

    assert name == "🇺🇸 США (xHTTP)"


def test_alternate_profile_name_falls_back_to_transport_variant():
    node = SimpleNamespace(region="RU|Россия")
    config = SimpleNamespace(config={})

    name = alternate_profile_name(
        node,
        config,
        "vless://uuid@176.108.250.142:23691?security=reality#REALITY-2",
        0,
        "Reality 2",
        23691,
    )

    assert name == "🇷🇺 Россия (Reality)"


def test_incy_allows_labeled_ru_cascade_reality_profiles():
    assert incy_profile_allowed(
        "vless://uuid@176.108.250.142:443?security=reality#REALITY",
        "RU|Россия",
        "🇺🇸 США (Reality)",
    )
    assert incy_profile_allowed(
        "vless://uuid@176.108.250.142:443?security=reality#REALITY-2",
        "RU|Россия",
        "🇵🇱 Польша (Reality)",
    )
    assert incy_profile_allowed(
        "vless://uuid@176.108.250.142:443?type=xhttp&security=reality#US-XHTTP",
        "RU|Россия",
        "🇺🇸 США (xHTTP)",
    )
    assert incy_profile_allowed(
        "vless://uuid@176.108.250.142:443?type=grpc&security=reality#FI-gRPC",
        "RU|Россия",
        "🇫🇮 Финляндия (gRPC)",
    )


def test_kit_profile_names_use_country_and_actual_transport():
    node = SimpleNamespace(region="RU|Россия")
    config = SimpleNamespace(config={})

    expected = {
        "US-XHTTP": (
            "vless://uuid@176.108.250.142:443?type=xhttp&security=reality#US-XHTTP-vpn-140",
            "🇺🇸 США (xHTTP)",
        ),
        "PL-gRPC": (
            "vless://uuid@176.108.250.142:443?type=grpc&security=reality#PL-gRPC-vpn-140",
            "🇵🇱 Польша (gRPC)",
        ),
        "FI-REALITY": (
            "vless://uuid@176.108.250.142:443?type=tcp&security=reality#FI-REALITY-vpn-140",
            "🇫🇮 Финляндия (Reality)",
        ),
    }
    for _key, (uri, label) in expected.items():
        assert localized_profile_name(node, uri, 0) == label
        assert profile_name_for_node(node, config, uri, 0) == label
        assert incy_profile_allowed(uri, node.region, label)


def test_incy_uses_uri_transport_not_display_label():
    uri = "vless://uuid@176.108.250.142:443?type=grpc&security=reality#FI-gRPC"
    assert incy_profile_allowed(uri, "RU|Россия", "🇺🇸 США (Reality)")
    unsupported = "vless://uuid@176.108.250.142:443?type=tcp&security=none#US-RAW"
    assert not incy_profile_allowed(unsupported, "RU|Россия", "🇺🇸 США (Reality)")


def test_profile_deduplication_ignores_label_but_keeps_transport_parameters():
    first = "vless://uuid@176.108.250.142:443?security=reality&type=grpc#US-gRPC"
    duplicate = "vless://uuid@176.108.250.142:443?type=grpc&security=reality#🇺🇸 США (gRPC)"
    other = "vless://uuid@176.108.250.142:443?type=grpc&security=reality&sni=pl.example#PL-gRPC"

    assert profile_identity(first) == profile_identity(duplicate)
    assert profile_identity(first) != profile_identity(other)
    assert len(deduplicate_profiles([{"config": first}, {"config": duplicate}, {"config": other}])) == 2
