"""Barra factor-risk objective and linear portfolio constraints.

The risk inputs are point-in-time estimates supplied by the data owner:
asset covariance = exposure @ factor covariance @ exposure.T + specific variance.
"""
import numpy as np


def _dated(dataset, date, name):
    if dataset.status != "available":
        raise ValueError(f"{name} is unavailable: {dataset.reason}")
    frame = dataset.data
    if not frame.date.eq(date).all():
        raise ValueError(f"{name} must belong to the signal date")
    return frame


def _aligned(dataset, date, codes, name, columns):
    frame = _dated(dataset, date, name)
    if frame.code.duplicated().any():
        raise ValueError(f"{name} contains duplicate stocks")
    indexed = frame.set_index("code")
    if not set(codes).issubset(indexed.index):
        raise ValueError(f"{name} does not cover the legal universe")
    return indexed.loc[codes, list(columns)]


def asset_covariance(*, config, date, codes, inputs):
    """Align real risk inputs; never synthesize missing factor/specific risks."""
    factors = list(config.barra_factors)
    exposures = _aligned(inputs.barra_exposures, date, codes, "Barra exposures", factors).to_numpy(dtype=float)
    frame = _dated(inputs.factor_covariance, date, "factor covariance")
    factor_cov = frame.pivot(index="factor1", columns="factor2", values="covariance").reindex(
        index=factors, columns=factors).to_numpy(dtype=float)
    specific = _aligned(inputs.specific_risk, date, codes, "specific risk",
                        ("specific_variance",)).to_numpy(dtype=float).ravel()
    if not all(np.isfinite(array).all() for array in (exposures, factor_cov, specific)):
        raise ValueError("Barra exposures and risk parameters must be complete finite numbers")
    if (specific < 0).any():
        raise ValueError("specific variances must be nonnegative")
    if not np.allclose(factor_cov, factor_cov.T, rtol=1e-10, atol=1e-12):
        raise ValueError("factor covariance must be symmetric")
    factor_cov = (factor_cov + factor_cov.T) / 2
    smallest = np.linalg.eigvalsh(factor_cov).min()
    tolerance = 1e-12 * max(1.0, np.abs(factor_cov).max())
    if smallest < -tolerance:
        raise ValueError("factor covariance must be positive semidefinite")
    if smallest < 0:  # Remove only floating-point roundoff, not invalid risk estimates.
        factor_cov = factor_cov + np.eye(len(factors)) * -smallest
    covariance = exposures @ factor_cov @ exposures.T + np.diag(specific)
    if not np.isfinite(covariance).all():
        raise ValueError("asset covariance overflow")
    return covariance, exposures


def optimize_barra(*, config, date, codes, alpha, benchmark, inputs, current_weights):
    try:
        from scipy.optimize import linprog, minimize
    except ImportError as exc:
        raise ImportError("method=barra requires skd-backtest[optimizer]") from exc
    if not codes:
        raise ValueError("Barra optimization requires a nonempty legal universe")
    covariance, exposures = asset_covariance(config=config, date=date, codes=codes, inputs=inputs)
    count = len(codes)
    b = np.array([benchmark[code] for code in codes], dtype=float)
    # Centered percentile ranks keep the signal invariant to strictly increasing transforms.
    signal = np.array([alpha[code] - 0.5 for code in codes], dtype=float)
    current_map = dict(zip(current_weights.code, current_weights.weight))
    current = np.array([current_map.get(code, 0.0) for code in codes], dtype=float)
    outside = sum(float(weight) for code, weight in current_map.items() if code not in set(codes))
    if not np.isfinite(current).all() or (current < 0).any() or outside < 0:
        raise ValueError("current cash-equity weights must be finite and nonnegative")
    use_turnover = config.turnover_limit is not None
    size = count * (2 if use_turnover else 1)
    lower = np.zeros(size)
    upper = np.full(size, np.inf)
    upper[:count] = config.single_name_weight_limit or 1.0
    if config.active_weight_limit is not None:
        lower[:count] = np.maximum(lower[:count], b - config.active_weight_limit)
        upper[:count] = np.minimum(upper[:count], b + config.active_weight_limit)
    if (lower > upper).any():
        raise ValueError("portfolio constraints are infeasible: incompatible weight bounds")

    rows, limits = [], []
    equal_rows, equal_values = [], []
    def inequality(row, limit):
        rows.append(row)
        limits.append(limit)

    budget = np.zeros(size)
    budget[:count] = 1
    if config.fully_invested:
        equal_rows.append(budget)
        equal_values.append(1.0)
    else:
        inequality(budget, 1.0)

    def exposure_limits(matrix, limit):
        for exposure in matrix.T:
            row = np.zeros(size)
            row[:count] = exposure
            center = exposure @ b
            if limit == 0:
                equal_rows.append(row)
                equal_values.append(center)
            else:
                inequality(row, center + limit)
                inequality(-row, -center + limit)

    if config.industry_exposure_limit is not None:
        industries = _aligned(inputs.industries, date, codes, "industries", ("industry",))
        labels = industries.industry.tolist()
        if any(not isinstance(label, str) or not label for label in labels):
            raise ValueError("industry labels must be nonempty strings")
        matrix = np.array([[label == industry for industry in sorted(set(labels))]
                           for label in labels], dtype=float)
        exposure_limits(matrix, config.industry_exposure_limit)
    if config.barra_style_exposure_limit is not None:
        exposure_limits(exposures, config.barra_style_exposure_limit)
    if use_turnover:
        # Two-sided asset turnover; forced exits count even when outside today's index.
        remaining = config.turnover_limit - outside
        if remaining < -1e-10:
            raise ValueError("portfolio constraints are infeasible: exits exceed turnover limit")
        for index in range(count):
            row = np.zeros(size)
            row[index], row[count + index] = 1, -1
            inequality(row, current[index])
            row = np.zeros(size)
            row[index], row[count + index] = -1, -1
            inequality(row, -current[index])
        row = np.zeros(size)
        row[count:] = 1
        inequality(row, max(remaining, 0.0))

    matrix = np.array(rows).reshape(-1, size)
    rhs = np.array(limits)
    # Exact sector/style neutrality can duplicate the budget (or each other).
    # Keep an independent equality basis so SLSQP does not falsely stop at its seed.
    basis, values = [], []
    for row, value in zip(equal_rows, equal_values):
        if basis:
            coefficients = np.linalg.lstsq(np.array(basis).T, row, rcond=1e-12)[0]
            residual = row - coefficients @ np.array(basis)
            implied = coefficients @ np.array(values)
        else:
            residual, implied = row, 0.0
        if np.linalg.norm(residual) > 1e-10 * max(1.0, np.linalg.norm(row)):
            basis.append(row)
            values.append(value)
        elif abs(value - implied) > 1e-9:
            raise ValueError("portfolio constraints are infeasible: inconsistent equalities")
    equality = np.array(basis) if basis else None
    equal_rhs = np.array(values)
    feasibility = linprog(
        np.zeros(size), A_ub=matrix if rows else None, b_ub=rhs if rows else None,
        A_eq=equality, b_eq=equal_rhs if equality is not None else None,
        bounds=list(zip(lower, upper)), method="highs",
    )
    if not feasibility.success:
        raise ValueError(f"portfolio constraints are infeasible: {feasibility.message}")

    def objective(z):
        active = z[:count] - b
        return 0.5 * config.risk_aversion * (active @ covariance @ active) - signal @ z[:count]

    def gradient(z):
        result = np.zeros(size)
        result[:count] = config.risk_aversion * covariance @ (z[:count] - b) - signal
        return result

    constraints = []
    if rows:
        constraints.append({"type": "ineq", "fun": lambda z: rhs - matrix @ z,
                            "jac": lambda z: -matrix})
    if equality is not None:
        constraints.append({"type": "eq", "fun": lambda z: equality @ z - equal_rhs,
                            "jac": lambda z: equality})
    solution = minimize(
        objective, feasibility.x, jac=gradient, method="SLSQP",
        bounds=list(zip(lower, upper)), constraints=constraints,
        options={"ftol": 1e-11, "maxiter": 1000},
    )
    if not solution.success or not np.isfinite(solution.x).all():
        raise ValueError(f"Barra optimization failed: {solution.message}")
    weights = np.clip(solution.x[:count], 0, 1)
    if weights.sum() > 1:
        weights /= weights.sum()  # Never publish leverage due to solver roundoff.
    verified = np.r_[weights, np.abs(weights - current)] if use_turnover else weights
    tolerance = 1e-8
    if ((verified < lower - tolerance).any() or (verified > upper + tolerance).any()
            or (rows and (matrix @ verified > rhs + tolerance).any())
            or (equality is not None and (np.abs(equality @ verified - equal_rhs) > tolerance).any())):
        raise ValueError("Barra solution violates portfolio constraints")
    return dict(zip(codes, map(float, weights)))
