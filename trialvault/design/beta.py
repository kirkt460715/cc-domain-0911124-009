import math


def _betacf(a, b, x):
    MAXIT = 300
    EPS = 3e-14
    FPMIN = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta) < EPS:
            return h
    return h


def betai(a, b, x):
    """正则化不完全 beta 函数 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(t, df):
    """Student t 累积分布，双侧尾部使用 I_x。"""
    if df <= 0:
        raise ValueError("degrees of freedom must be positive")
    x = df / (df + t * t)
    tail = betai(df / 2.0, 0.5, x)
    return 1.0 - tail / 2.0 if t >= 0 else tail / 2.0


def t_quantile(probability, df):
    """Student t 分位数，二分求解 t_cdf。"""
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must lie strictly between zero and one")
    if probability < 0.5:
        return -t_quantile(1.0 - probability, df)
    low, high = 0.0, 1.0
    while t_cdf(high, df) < probability:
        high *= 2.0
        if high > 1e6:
            raise ValueError("t quantile did not converge")
    for _ in range(200):
        middle = (low + high) / 2.0
        if t_cdf(middle, df) < probability:
            low = middle
        else:
            high = middle
        if high - low < 1e-12:
            break
    return (low + high) / 2.0


def normal_cdf(value):
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def normal_quantile(probability):
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must lie strictly between zero and one")
    if probability < 0.5:
        return -normal_quantile(1.0 - probability)
    low, high = 0.0, 1.0
    while normal_cdf(high) < probability:
        high *= 2.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if normal_cdf(middle) < probability:
            low = middle
        else:
            high = middle
        if high - low < 1e-14:
            break
    return (low + high) / 2.0
