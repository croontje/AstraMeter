// Mechanical mirror of src/astrameter/ct002/energy_policy.py.
// Shared by the Python and firmware CT002 control loops.
#pragma once

#include <optional>

namespace esphome {
namespace ct002 {

struct EnergyPolicyConfig {
  double threshold_w{2400.0};
  double max_charge_w{800.0};
  double max_discharge_w{800.0};
  int charge_start_minute{0};
  int charge_end_minute{360};
  bool night_charge_enabled{false};
  bool quarter_enabled{false};
  double response_margin_s{10.0};
  double night_charge_power_w{800.0};
  bool valid() const;
};

struct QuarterReading {
  double accumulated_w;
  double sample_epoch;
};

struct MonthlyPeakReading {
  double power_w;
  double timestamp;  // Local meter YYMMDDhhmmss, not receipt time.
  double sample_epoch;
};
bool valid_peak_timestamp(double value);

class QuarterTracker {
 public:
  std::optional<QuarterReading> feed(double accumulated_w, double import_kwh,
                                    double epoch, double monotonic);
 private:
  struct Point { double q, imported, epoch, mono; };
  std::optional<Point> previous_;
  std::optional<Point> anchor_;
  double bad_window_{-1};
};

struct EnergyDecision {
  double battery_target_w;
  double grid_target_w;
  double correction_w;
  double grid_limit_w;
  bool night_window;
  bool quarter_used;
  bool limited;
  double effective_threshold_w;
  bool monthly_peak_used;
};

bool in_charge_window(int minute, int start, int end);
// nullopt is the Python ValueError equivalent: the caller must enter the
// existing meter-failure path, not turn missing data into a zero measurement.
std::optional<EnergyDecision> decide_energy(
    const EnergyPolicyConfig &config, double grid_w, double battery_w,
    double epoch, std::optional<int> local_minute,
    std::optional<QuarterReading> quarter = std::nullopt,
    std::optional<MonthlyPeakReading> monthly_peak = std::nullopt,
    std::optional<double> local_timestamp = std::nullopt);

}  // namespace ct002
}  // namespace esphome
