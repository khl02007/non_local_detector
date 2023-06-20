import jax
import jax.numpy as jnp
import numpy as np
import scipy.stats
from patsy import build_design_matrices, dmatrix
from scipy.optimize import minimize
from tqdm.autonotebook import tqdm

from non_local_detector.core import atleast_2d
from non_local_detector.environment import get_n_bins

EPS = 1e-15


def make_spline_design_matrix(
    position: np.ndarray, place_bin_edges: np.ndarray, knot_spacing: float = 10.0
):
    position = atleast_2d(position)
    inner_knots = []
    for pos, edges in zip(position.T, place_bin_edges.T):
        n_points = get_n_bins(edges, bin_size=knot_spacing)
        knots = np.linspace(edges.min(), edges.max(), n_points)[1:-1]
        knots = knots[(knots > pos.min()) & (knots < pos.max())]
        inner_knots.append(knots)

    inner_knots = np.meshgrid(*inner_knots)

    data = {}
    formula = "1 + te("
    for ind in range(position.shape[1]):
        formula += f"cr(x{ind}, knots=inner_knots[{ind}])"
        formula += ", "
        data[f"x{ind}"] = position[:, ind]

    formula += 'constraints="center")'
    return dmatrix(formula, data)


def make_spline_predict_matrix(design_info, position: np.ndarray):
    position = atleast_2d(position)
    is_nan = np.any(np.isnan(position), axis=1)
    position[is_nan] = 0.0

    predict_data = {}
    for ind in range(position.shape[1]):
        predict_data[f"x{ind}"] = position[:, ind]

    design_matrix = build_design_matrices([design_info], predict_data)[0]
    design_matrix[is_nan] = np.nan

    return design_matrix


def fit_poisson_regression(
    design_matrix: np.ndarray,
    spikes: np.ndarray,
    weights: np.ndarray,
    l2_penalty: float = 1e-7,
):
    @jax.jit
    def neglogp(
        coefficients, spikes=spikes, design_matrix=design_matrix, weights=weights
    ):
        conditional_intensity = jnp.exp(design_matrix @ coefficients)
        conditional_intensity = jnp.clip(conditional_intensity, a_min=EPS, a_max=None)
        negative_log_likelihood = -1.0 * jnp.mean(
            weights * jax.scipy.stats.poisson.logpmf(spikes, conditional_intensity)
        )
        l2_penalty_term = l2_penalty * jnp.sum(coefficients[1:] ** 2)
        return negative_log_likelihood + l2_penalty_term

    dlike = jax.grad(neglogp)

    initial_condition = np.array([np.log(np.average(spikes, weights=weights))])
    initial_condition = np.concatenate(
        [initial_condition, np.zeros(design_matrix.shape[1] - 1)]
    )

    res = minimize(
        neglogp,
        x0=initial_condition,
        method="BFGS",
        jac=dlike,
    )

    return res.x


def fit_sorted_spikes_glm_jax_encoding_model(
    position: np.ndarray,
    spikes: np.ndarray,
    place_bin_centers: np.ndarray,
    place_bin_edges: np.ndarray,
    edges: np.ndarray,
    is_track_interior: np.ndarray,
    is_track_boundary: np.ndarray,
    emission_knot_spacing: float = 10.0,
    l2_penalty: float = 1e-3,
):
    emission_design_matrix = make_spline_design_matrix(
        position, place_bin_edges, knot_spacing=emission_knot_spacing
    )
    emission_predict_matrix = make_spline_predict_matrix(
        emission_design_matrix.design_info, place_bin_centers
    )
    weights = np.ones((spikes.shape[0],), dtype=np.float32)

    coefficients = []
    place_fields = []
    for neuron_spikes in tqdm(spikes.T):
        coef = fit_poisson_regression(
            emission_design_matrix,
            neuron_spikes,
            weights,
            l2_penalty=l2_penalty,
        )
        coefficients.append(coef)

        place_field = np.exp(emission_predict_matrix @ coef)
        place_field[~is_track_interior] = EPS
        place_field = np.clip(place_field, a_min=EPS, a_max=None)
        place_fields.append(place_field)

    return {
        "coefficients": np.stack(coefficients, axis=0),
        "emission_design_info": emission_design_matrix.design_info,
        "place_fields": np.stack(place_fields, axis=0),
        "is_track_interior": is_track_interior,
    }


def predict_sorted_spikes_glm_jax_log_likelihood(
    position: np.ndarray,
    spikes: np.ndarray,
    coefficients: np.ndarray,
    emission_design_info,
    place_fields: np.ndarray,
    is_track_interior: np.ndarray,
    is_local: bool = False,
):
    n_time = spikes.shape[0]
    if is_local:
        log_likelihood = np.zeros((n_time,))
        emission_predict_matrix = make_spline_predict_matrix(
            emission_design_info, position
        )
        for neuron_spikes, coef in zip(tqdm(spikes.T), coefficients):
            local_rate = np.exp(emission_predict_matrix @ coef)
            local_rate = np.clip(local_rate, a_min=EPS, a_max=None)
            log_likelihood += scipy.stats.poisson.logpmf(neuron_spikes, local_rate)

        log_likelihood = log_likelihood[:, np.newaxis]
    else:
        log_likelihood = np.zeros((n_time, place_fields.shape[1]))
        for neuron_spikes, place_field in zip(tqdm(spikes.T), place_fields):
            log_likelihood += scipy.stats.poisson.logpmf(
                neuron_spikes[:, np.newaxis], place_field[np.newaxis]
            )
        log_likelihood[:, ~is_track_interior] = np.nan

    return log_likelihood
