#include "energy_policy.h"

#include <algorithm>
#include <cmath>
#include <cstdint>

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

bool valid_peak_timestamp(double value) {
  if (!std::isfinite(value) || value < 0 || value >= 1e12 || value != std::floor(value)) return false;
  const auto stamp = static_cast<int64_t>(value);
  const int year = 2000 + stamp / 10000000000LL, month = stamp / 100000000 % 100;
  const int day = stamp / 1000000 % 100, hour = stamp / 10000 % 100;
  const int minute = stamp / 100 % 100, second = stamp % 100;
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return false;
  const int days[] = {31, year % 4 == 0 ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  return day >= 1 && day <= days[month-1];
}

std::optional<EnergyDecision> decide_energy(
    const EnergyPolicyConfig &config, double grid_w, double battery_w,
    double epoch, std::optional<int> local_minute, std::optional<QuarterReading> quarter,
    std::optional<MonthlyPeakReading> monthly_peak, std::optional<double> local_timestamp) {
  if (!config.valid() || !std::isfinite(grid_w) || !std::isfinite(battery_w) ||
      !std::isfinite(epoch) || epoch < 0) return std::nullopt;
  const double demand = grid_w + battery_w;
  if (!std::isfinite(demand)) return std::nullopt;
  const bool night = config.night_charge_enabled && local_minute.has_value() &&
      in_charge_window(*local_minute, config.charge_start_minute, config.charge_end_minute);
  double threshold = config.threshold_w;
  if (config.quarter_enabled && monthly_peak && local_timestamp) {
    const auto &p = *monthly_peak;
    if (std::isfinite(p.power_w) && p.power_w >= 0 && std::isfinite(p.sample_epoch) && p.sample_epoch >= 0 &&
        epoch-p.sample_epoch >= 0 && epoch-p.sample_epoch <= 3 &&
        valid_peak_timestamp(p.timestamp) && valid_peak_timestamp(*local_timestamp) &&
        std::floor(p.timestamp / 100000000) == std::floor(*local_timestamp / 100000000) &&
        p.timestamp <= *local_timestamp) {
      threshold = std::max(threshold, p.power_w - 100.0);
    }
  }
  double limit = threshold;
  bool quarter_used = false;
  if (config.quarter_enabled && quarter.has_value()) {
    const double q = quarter->accumulated_w, sampled = quarter->sample_epoch;
    if (std::isfinite(q) && q >= 0 && std::isfinite(sampled) && sampled >= 0 &&
        epoch - sampled >= 0 && epoch - sampled <= 3 &&
        std::floor(epoch / 900) == std::floor(sampled / 900)) {
      const double remaining = 900 - std::fmod(epoch, 900);
      double budget_ws = (threshold - q) * 900;
      const double delay = std::min(config.response_margin_s, remaining);
      budget_ws -= std::max(0.0, grid_w) * (epoch - sampled + delay);
      if (remaining > delay) {
        limit = std::max(0.0, budget_ws / (remaining - delay));
      } else {
        limit = budget_ws >= 0 ? threshold : 0.0;
      }
      quarter_used = true;
    }
  }
  double requested = std::max(demand - limit, night ? -std::min(config.night_charge_power_w, config.max_charge_w) : 0.0);
  if (demand < 0) requested = std::min(requested, demand);
  const double target = std::max(-config.max_charge_w, std::min(config.max_discharge_w, requested));
  const double net_target = demand - target;
  return EnergyDecision{target, net_target, grid_w - net_target, limit,
                        night, quarter_used, std::abs(requested - target) > 1e-6,
                        threshold, threshold > config.threshold_w};
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
    // A delayed normal quarter reset is expected during settling. Compare
    // decreases only when both samples are settled; cumulative import must
    // remain monotonic even during settling. Mirrors QuarterTracker.feed.
    const bool quarter_reset = std::fmod(epoch, 900) >= 15 &&
        std::fmod(p.epoch, 900) >= 15 && q < p.q-1;
    if (std::floor(p.epoch / 900) == window && (quarter_reset || imported < p.imported)) bad_window_ = window;
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
