"""Exact affine coordinate conversion; wire values contain no floats."""

from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction
from math import ceil, floor, gcd


def millipoints(value):
    return int((Decimal(str(value)) * 1000).to_integral_value(rounding=ROUND_HALF_EVEN))


def rational(value):
    f = Fraction(value)
    return {"numerator": f.numerator, "denominator": f.denominator}


def matrix(values):
    return [rational(v) for v in values]


def coefficients(values):
    if len(values) != 9:
        raise ValueError("affine matrix must have nine entries")
    result = []
    for v in values:
        n, d = v["numerator"], v["denominator"]
        if type(n) is not int or type(d) is not int or d <= 0 or gcd(n, d) != 1:
            raise ValueError("rational must be reduced with positive denominator")
        result.append(Fraction(n, d))
    a, b, c, d, e, f, g, h, i = result
    if (g, h, i) != (0, 0, 1) or a * e - b * d == 0:
        raise ValueError("invalid or singular affine matrix")
    return result


def project(polygon, transform):
    a, b, c, d, e, f, _, _, _ = coefficients(transform)
    exact = [(a * x + b * y + c, d * x + e * y + f) for x, y in polygon]
    return (
        [[round(x), round(y)] for x, y in exact],
        [
            floor(min(x for x, y in exact)),
            floor(min(y for x, y in exact)),
            ceil(max(x for x, y in exact)),
            ceil(max(y for x, y in exact)),
        ],
    )


def rectangle(box):
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
