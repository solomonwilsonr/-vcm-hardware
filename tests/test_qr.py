import pytest

from vcm_trace.qr import _format_bits, make_qr, qr_svg, rs_remainder


def test_format_bits_known_values():
    # ISO 18004 format string for level M, mask 0 (after XOR with 101010000010010)
    assert _format_bits(0) == 0b101010000010010
    assert _format_bits(1) == 0b101000100100101


def test_reed_solomon_known_vector():
    # classic example: data of "HELLO WORLD" version 1-M style check via polynomial division property
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    ec = rs_remainder(data, 10)
    assert ec == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


def test_sizes_grow_with_payload_and_structure():
    assert len(make_qr("hi")) == 21
    assert len(make_qr("x" * 30)) == 29
    assert len(make_qr("x" * 120)) == 45  # version 8 level M
    m = make_qr("https://vcm.example.com/device/VCM-0001")
    n = len(m)
    # finder pattern corners and the dark module
    assert all(m[0][i] for i in range(7)) and all(m[6][i] for i in range(7))
    assert m[n - 8][8] is True
    # timing pattern alternates
    assert [m[6][x] for x in range(8, n - 8)] == [x % 2 == 0 for x in range(8, n - 8)]


def test_deterministic_and_too_long():
    assert make_qr("abc") == make_qr("abc")
    with pytest.raises(ValueError):
        make_qr("x" * 400)


def test_svg():
    svg = qr_svg("vcm")
    assert svg.startswith("<svg") and "<path" in svg and svg.rstrip().endswith("</svg>")


def test_decodes_with_opencv_if_available():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    for text in ("https://vcm.example.com/device/VCM-0001", "x" * 150):
        m = make_qr(text)
        n, s, b = len(m), 8, 4
        img = np.full(((n + 2 * b) * s,) * 2, 255, np.uint8)
        for y in range(n):
            for x in range(n):
                if m[y][x]:
                    img[(y + b) * s:(y + b + 1) * s, (x + b) * s:(x + b + 1) * s] = 0
        assert cv2.QRCodeDetector().detectAndDecode(img)[0] == text
