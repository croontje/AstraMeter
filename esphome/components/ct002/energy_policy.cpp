#include "energy_policy.h"

#include <algorithm>
#include <cmath>

namespace esphome {
namespace ct002 {

bool EnergyPolicyConfig::valid() const {
  return std::isfinite(threshold_w) && threshold_w > 0 &&
         std::isfinite(max_charge_w) && max_charge_w >= 0 &&
         std::isfinite(max_discharge_w) && max_discharge_w >= 0 &&
         std::isfinite(night_charge_power_w) && night_charge_power_w >= 0 &&
         charge_start_minute >= 0 && charge_start_minute < 1440 &&
         charge_end_minute >= 0 && charge_end_minute < 1440 &&
         std::isfinite(response_margin_s) && response_margin_s >= 0 && response_margin_s < 900;
}

bool in_charge_window(int minute, int start, int end) {
  if (minute < 0 || minute >= 1440 || start == end) return false;
  if (start < end) return start <= minute && minute < end;
  return minute >= start || minute < end;
}

std::optional<EnergyDecision> decide_energy(
    const EnergyPolicyConfig &config, double grid_w, double battery_w,
    double epoch, std::optional<int> local_minute, std::optional<QuarterReading> quarter) {
  if (!config.valid() || !std::isfinite(grid_w) || !std::isfinite(battery_w) ||
      !std::isfinite(epoch) || epoch < 0) return std::nullopt;
  const double demand = grid_w + battery_w;
  if (!std::isfinite(demand)) return std::nullopt;
  const bool night = config.night_charge_enabled && local_minute.has_value() &&
      in_charge_window(*local_minute, config.charge_start_minute, config.charge_end_minute);
  double limit = config.threshold_w;
  bool quarter_used = false;
  if (config.quarter_enabled && quarter.has_value()) {
    const double q = quarter->accumulated_w, sampled = quarter->sample_epoch;
    if (std::isfinite(q) && q >= 0 && std::isfinite(sampled) && sampled >= 0 &&
        epoch - sampled >= 0 && epoch - sampled <= 3 &&
        std::floor(epoch / 900) == std::floor(sampled / 900)) {
      const double remaining = 900 - std::fmod(epoch, 900);
      double budget_ws = (config.threshold_w - q) * 900;
      const double delay = std::min(config.response_margin_s, remaining);
      budget_ws -= std::max(0.0, grid_w) * (epoch - sampled + delay);
      if (remaining > delay) {
        limit = std::max(0.0, budget_ws / (remaining - delay));
      } else {
        limit = budget_ws >= 0 ? config.threshold_w : 0.0;
      }
      quarter_used = true;
    }
  }
  double requested = std::max(demand - limit, night ? -std::min(config.night_charge_power_w, config.max_charge_w) : 0.0);
  if (demand < 0) requested = std::min(requested, demand);
  const double target = std::max(-config.max_charge_w, std::min(config.max_discharge_w, requested));
  const double net_target = demand - target;
  return EnergyDecision{target, net_target, grid_w - net_target, limit,
                        night, quarter_used, std::abs(requested - target) > 1e-6};
}

std::optional<QuarterReading> QuarterTracker::feed(double q, double imported, double epoch, double mono) {
  if (!std::isfinite(q) || q < 0 || !std::isfinite(imported) || imported < 0 ||
      !std::isfinite(epoch) || epoch < 0 || !std::isfinite(mono) || mono < 0) {
    previous_.reset(); anchor_.reset(); return std::nullopt;
  }
  const double window = std::floor(epoch / 900);
  const Point point{q, imported, epoch, mono};
  if (previous_) {
    const auto &p = *previous_;
    if (mono < p.mono || mono-p.mono > 10 || std::abs(epoch-p.epoch-mono+p.mono) > 2) anchor_.reset();
    if (std::floor(p.epoch / 900) == window && (q < p.q-1 || imported < p.imported)) bad_window_ = window;
  }
  previous_ = point;
  if (std::fmod(epoch, 900) < 15 || bad_window_ == window) {
    anchor_.reset(); return std::nullopt;
  }
  if (!anchor_ || std::floor(anchor_->epoch / 900) != window) anchor_ = point;
  const auto &a = *anchor_;
  if (std::abs(q-a.q-(imported-a.imported)*4000) > 8) {
    anchor_ = point; return std::nullopt;
  }
  if (mono-a.mono < 30) return std::nullopt;
  return QuarterReading{q, epoch};
}

}  // namespace ct002
}  // namespace esphome
