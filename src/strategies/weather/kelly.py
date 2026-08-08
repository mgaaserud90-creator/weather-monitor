"""
Kelly Criterion position sizing for weather strategy.

Implements:
  - Standard Kelly:  f* = (p*b - q) / b
  - Quarter-Kelly:   f* / 4  (conservative default)
  - Half-Kelly:      f* / 2
  - Fractional Kelly with configurable fraction
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KellyResult:
    """Result of a Kelly calculation."""
    win_probability: float      # p: probability of winning
    net_odds: float             # b: net profit / stake (decimal odds - 1)
    kelly_fraction: float       # f*: full Kelly fraction
    recommended_fraction: float  # Fraction after applying Kelly multiplier
    recommended_size_usdc: float  # Absolute position size in USDC
    is_valid: bool = True


class KellySizer:
    """
    Kelly Criterion for position sizing.

    f* = (p*b - q) / b
    where:
      p = win probability
      q = 1 - p
      b = net odds (net profit / stake)

    For Polymarket binary outcomes at price 'price':
      b = (1 - price) / price  (if buying YES)
      b = price / (1 - price)  (if buying NO)

    We default to quarter-Kelly (f*/4) for conservatism.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_capital_fraction: float = 0.25,
    ) -> None:
        """
        Args:
            kelly_fraction: Fraction of full Kelly to use (0.25 = quarter-Kelly)
            max_capital_fraction: Absolute cap on capital allocation (0.25 = max 25%)
        """
        self._kelly_fraction = kelly_fraction
        self._max_capital_fraction = max_capital_fraction

    # =========================================================================
    # Main calculation
    # =========================================================================

    def calculate(
        self,
        win_prob: float,
        net_odds: float,
        capital: float = 10000.0,
    ) -> KellyResult:
        """
        Calculate recommended position size using fractional Kelly.

        Args:
            win_prob: Probability of winning (0.0–1.0)
            net_odds: Net odds (b): how much profit per unit staked.
                      For buying at price p: b = (1-p)/p
            capital: Total available capital in USDC

        Returns:
            KellyResult with recommended fraction and size.
        """
        if win_prob <= 0 or net_odds <= 0:
            return KellyResult(
                win_probability=win_prob,
                net_odds=net_odds,
                kelly_fraction=0.0,
                recommended_fraction=0.0,
                recommended_size_usdc=0.0,
                is_valid=False,
            )

        q = 1.0 - win_prob

        # Full Kelly: f* = (p*b - q) / b
        full_kelly = (win_prob * net_odds - q) / net_odds

        # Kelly can be negative (no bet) or very large for extreme edges
        full_kelly = max(0.0, full_kelly)

        # Apply fractional Kelly multiplier
        fractional = full_kelly * self._kelly_fraction

        # Cap at max capital fraction
        recommended = min(fractional, self._max_capital_fraction)

        # Compute absolute size
        size = capital * recommended

        return KellyResult(
            win_probability=win_prob,
            net_odds=net_odds,
            kelly_fraction=full_kelly,
            recommended_fraction=recommended,
            recommended_size_usdc=round(size, 2),
            is_valid=True,
        )

    # =========================================================================
    # Convenience: from Polymarket price
    # =========================================================================

    @staticmethod
    def odds_from_price(price: float, side: str = "BUY") -> float:
        """
        Convert Polymarket price to net odds.

        For BUY YES at price p:
          - If you win: you get $1, profit = (1-p), stake = p
          - net_odds = (1-p) / p

        For BUY NO at price p (which means price of NO token):
          - NO token pays $1 if NO wins
          - Same formula with p_NO

        Args:
            price: Polymarket token price (0.0–1.0)
            side: "BUY" or "SELL"

        Returns:
            Net odds (b)
        """
        if price <= 0 or price >= 1:
            return 0.0

        if side.upper() == "BUY":
            return (1.0 - price) / price
        else:
            # Selling: you receive price now, risk (1-price) if wrong
            return price / (1.0 - price)

    @staticmethod
    def price_from_probability(
        win_prob: float,
        margin: float = 0.0,
    ) -> float:
        """
        Convert win probability to fair price.

        Args:
            win_prob: Model-estimated probability
            margin: Edge margin to add (e.g., 0.05 for 5% edge)

        Returns:
            Fair price (0.0–1.0)
        """
        return max(0.01, min(0.99, win_prob - margin))

    @property
    def kelly_fraction(self) -> float:
        return self._kelly_fraction

    @property
    def max_capital_fraction(self) -> float:
        return self._max_capital_fraction
