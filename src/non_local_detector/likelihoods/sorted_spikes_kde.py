from dataclasses import dataclass
from functools import partial

import jax
import jax.numpy as jnp
import scipy.interpolate
from tqdm.autonotebook import tqdm

EPS = 1e-15


@jax.jit
def gaussian_pdf(x: jnp.ndarray, mean: jnp.ndarray, sigma: jnp.ndarray) -> jnp.ndarray:
    """Compute the value of a Gaussian probability density function at x with
    given mean and sigma."""
    return jnp.exp(-0.5 * ((x - mean) / sigma) ** 2) / (sigma * jnp.sqrt(2.0 * jnp.pi))


@jax.jit
def kde(
    eval_points: jnp.ndarray, samples: jnp.ndarray, std: jnp.ndarray
) -> jnp.ndarray:
    distance = jnp.ones((samples.shape[0], eval_points.shape[0]))

    for dim_ind, std in enumerate(std):
        distance *= gaussian_pdf(
            jnp.expand_dims(eval_points[:, dim_ind], axis=0),
            jnp.expand_dims(samples[:, dim_ind], axis=1),
            std,
        )
    return jnp.mean(distance, axis=0).squeeze()


@partial(jax.jit, static_argnums=(3,))
def block_kde(
    eval_points: jnp.ndarray,
    samples: jnp.ndarray,
    std: jnp.ndarray,
    block_size: int = 100,
) -> jnp.ndarray:
    n_eval_points = eval_points.shape[0]
    density = jnp.zeros((n_eval_points,))
    for start_ind in range(0, n_eval_points, block_size):
        block_inds = slice(start_ind, start_ind + block_size)
        density = jax.lax.dynamic_update_slice(
            density,
            kde(eval_points[block_inds], samples, std).squeeze(),
            (start_ind,),
        )

    return density


@dataclass
class KDEModel:
    std: jnp.ndarray
    block_size: int | None = None

    def fit(self, samples: jnp.ndarray):
        samples = jnp.asarray(samples)
        if samples.ndim == 1:
            samples = jnp.expand_dims(samples, axis=1)
        self.samples_ = samples

        return self

    def predict(self, eval_points: jnp.ndarray):
        if eval_points.ndim == 1:
            eval_points = jnp.expand_dims(eval_points, axis=1)
        std = (
            jnp.array([self.std] * eval_points.shape[1])
            if isinstance(self.std, (int, float))
            else self.std
        )
        block_size = (
            eval_points.shape[0] if self.block_size is None else self.block_size
        )

        return block_kde(eval_points, self.samples_, std, block_size)


def fit_sorted_spikes_kde_encoding_model(
    position: jnp.ndarray,
    spikes: jnp.ndarray,
    place_bin_centers: jnp.ndarray,
    is_track_interior: jnp.ndarray,
    *args,
    position_std: float = 5.0,
    block_size: int = 100,
    disable_progress_bar: bool = False,
    **kwargs,
):
    occupancy_model = KDEModel(std=position_std, block_size=block_size).fit(position)
    occupancy = occupancy_model.predict(place_bin_centers[is_track_interior])
    mean_rates = jnp.mean(spikes, axis=0).squeeze()

    place_fields = []
    marginal_models = []

    for neuron_spikes, neuron_mean_rate in zip(
        tqdm(
            spikes.T.astype(bool),
            unit="cell",
            desc="Encoding models",
            disable=disable_progress_bar,
        ),
        mean_rates,
    ):
        neuron_marginal_model = KDEModel(std=position_std, block_size=block_size).fit(
            position[neuron_spikes]
        )
        marginal_models.append(neuron_marginal_model)
        marginal_density = neuron_marginal_model.predict(
            place_bin_centers[is_track_interior]
        )
        place_field = jnp.zeros((is_track_interior.shape[0],))
        place_fields.append(
            place_field.at[is_track_interior].set(
                jnp.clip(
                    neuron_mean_rate
                    * jnp.where(occupancy > 0.0, marginal_density / occupancy, EPS),
                    a_min=EPS,
                    a_max=None,
                )
            )
        )

    place_fields = jnp.stack(place_fields, axis=0)
    no_spike_part_log_likelihood = jnp.sum(place_fields, axis=0)

    return {
        "marginal_models": marginal_models,
        "occupancy_model": occupancy_model,
        "occupancy": occupancy,
        "mean_rates": mean_rates,
        "place_fields": place_fields,
        "no_spike_part_log_likelihood": no_spike_part_log_likelihood,
        "is_track_interior": is_track_interior,
        "disable_progress_bar": disable_progress_bar,
    }


def predict_sorted_spikes_kde_log_likelihood(
    position: jnp.ndarray,
    spikes: jnp.ndarray,
    marginal_models: list[KDEModel],
    occupancy_model: KDEModel,
    occupancy: jnp.ndarray,
    mean_rates: jnp.ndarray,
    place_fields: jnp.ndarray,
    no_spike_part_log_likelihood: jnp.ndarray,
    is_track_interior: jnp.ndarray,
    disable_progress_bar: bool = False,
    is_local: bool = False,
):
    n_time = spikes.shape[0]
    if is_local:
        log_likelihood = jnp.zeros((n_time,))

        occupancy = occupancy_model.predict(position)

        for neuron_spikes, neuron_marginal_model, neuron_mean_rate in zip(
            tqdm(
                spikes.T,
                unit="cell",
                desc="Local Likelihood",
                disable=disable_progress_bar,
            ),
            marginal_models,
            mean_rates,
        ):
            marginal_density = neuron_marginal_model.predict(position)
            local_rate = neuron_mean_rate * jnp.where(
                occupancy > 0.0, marginal_density / occupancy, EPS
            )
            local_rate = jnp.clip(local_rate, a_min=EPS, a_max=None)
            log_likelihood += (
                jax.scipy.special.xlogy(neuron_spikes, local_rate) - local_rate
            )

        log_likelihood = jnp.expand_dims(log_likelihood, axis=1)
    else:
        log_likelihood = jnp.zeros((n_time, place_fields.shape[1]))
        for neuron_spikes, place_field in zip(
            tqdm(
                spikes.T,
                unit="cell",
                desc="Non-Local Likelihood",
                disable=disable_progress_bar,
            ),
            place_fields,
        ):
            log_likelihood += jax.scipy.special.xlogy(
                neuron_spikes[:, jnp.newaxis], place_field[jnp.newaxis]
            )
        log_likelihood -= no_spike_part_log_likelihood[jnp.newaxis]
        log_likelihood = jnp.where(
            is_track_interior[jnp.newaxis, :], log_likelihood, jnp.log(EPS)
        )

    return log_likelihood
