#!/usr/bin/env python3
"""
Rate Limiter Module
Implements exponential backoff and request throttling for API calls
Prevents rate limit errors and data loss during high-volume periods
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable
from functools import wraps
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Track rate limit state for an API"""
    requests_made: int = 0
    window_start: float = 0.0
    last_request: float = 0.0
    backoff_until: float = 0.0
    consecutive_failures: int = 0


class RateLimiter:
    """Manages rate limiting and exponential backoff for API requests"""

    def __init__(self):
        self.limits: Dict[str, RateLimitState] = {}
        self.configs: Dict[str, dict] = {}

    def configure(self, api_name: str, requests_per_minute: int = 60,
                  min_delay_seconds: float = 0.0, max_backoff_seconds: int = 300):
        """
        Configure rate limits for an API
        
        Args:
            api_name: Unique identifier for the API
            requests_per_minute: Maximum requests allowed per minute
            min_delay_seconds: Minimum delay between requests
            max_backoff_seconds: Maximum backoff time (default 5 minutes)
        """
        self.configs[api_name] = {
            'requests_per_minute': requests_per_minute,
            'min_delay': min_delay_seconds,
            'max_backoff': max_backoff_seconds
        }
        self.limits[api_name] = RateLimitState(window_start=time.time())
        logger.info(f"Rate limiter configured for {api_name}: {requests_per_minute} req/min")

    def _should_throttle(self, api_name: str) -> tuple[bool, float]:
        """
        Check if request should be throttled
        
        Returns:
            Tuple of (should_wait, wait_seconds)
        """
        if api_name not in self.configs:
            return False, 0.0

        config = self.configs[api_name]
        state = self.limits[api_name]
        now = time.time()

        # Check if still in backoff period
        if state.backoff_until > now:
            wait_time = state.backoff_until - now
            return True, wait_time

        # Check minimum delay between requests
        if config['min_delay'] > 0:
            time_since_last = now - state.last_request
            if time_since_last < config['min_delay']:
                wait_time = config['min_delay'] - time_since_last
                return True, wait_time

        # Check rate limit window
        window_size = 60.0  # 1 minute in seconds
        if now - state.window_start >= window_size:
            # Reset window
            state.window_start = now
            state.requests_made = 0

        if state.requests_made >= config['requests_per_minute']:
            # Wait until window resets
            wait_time = window_size - (now - state.window_start)
            return True, wait_time

        return False, 0.0

    def wait_if_needed(self, api_name: str):
        """Block until rate limit allows request"""
        should_wait, wait_time = self._should_throttle(api_name)
        
        if should_wait and wait_time > 0:
            logger.warning(f"Rate limit: waiting {wait_time:.1f}s for {api_name}")
            time.sleep(wait_time)

    def record_request(self, api_name: str, success: bool = True):
        """Record that a request was made"""
        if api_name not in self.limits:
            return

        state = self.limits[api_name]
        state.requests_made += 1
        state.last_request = time.time()

        if success:
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1

    def trigger_backoff(self, api_name: str, error_code: Optional[int] = None):
        """
        Trigger exponential backoff after rate limit error
        
        Args:
            api_name: API that hit rate limit
            error_code: HTTP error code (429 = rate limit)
        """
        if api_name not in self.configs:
            return

        state = self.limits[api_name]
        config = self.configs[api_name]

        # Calculate exponential backoff: 2^failures seconds
        backoff_seconds = min(
            2 ** state.consecutive_failures,
            config['max_backoff']
        )

        state.backoff_until = time.time() + backoff_seconds
        state.consecutive_failures += 1

        logger.warning(
            f"Rate limit hit for {api_name} (error {error_code}). "
            f"Backing off for {backoff_seconds}s (failure #{state.consecutive_failures})"
        )

    def decorator(self, api_name: str):
        """
        Decorator for automatic rate limiting of functions
        
        Usage:
            limiter = RateLimiter()
            limiter.configure('alpha_vantage', requests_per_minute=5)
            
            @limiter.decorator('alpha_vantage')
            def fetch_news():
                return requests.get(...)
        """
        def decorator_func(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                # Wait if needed
                self.wait_if_needed(api_name)
                
                try:
                    # Execute function
                    result = func(*args, **kwargs)
                    
                    # Record success
                    self.record_request(api_name, success=True)
                    
                    return result
                    
                except Exception as e:
                    # Check if it's a rate limit error
                    if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                        if e.response.status_code == 429:
                            self.trigger_backoff(api_name, error_code=429)
                        else:
                            self.record_request(api_name, success=False)
                    else:
                        self.record_request(api_name, success=False)
                    
                    raise
            
            return wrapper
        return decorator_func

    def get_stats(self, api_name: str) -> dict:
        """Get current rate limit statistics"""
        if api_name not in self.limits:
            return {}

        state = self.limits[api_name]
        config = self.configs[api_name]
        now = time.time()

        return {
            'api_name': api_name,
            'requests_per_minute_limit': config['requests_per_minute'],
            'requests_this_window': state.requests_made,
            'window_resets_in': max(0, 60 - (now - state.window_start)),
            'in_backoff': state.backoff_until > now,
            'backoff_ends_in': max(0, state.backoff_until - now),
            'consecutive_failures': state.consecutive_failures,
            'seconds_since_last_request': now - state.last_request if state.last_request > 0 else None
        }


# Global rate limiter instance
_global_limiter = RateLimiter()


def configure_api(api_name: str, **kwargs):
    """Configure rate limits for an API using global limiter"""
    _global_limiter.configure(api_name, **kwargs)


def wait_if_needed(api_name: str):
    """Wait if rate limit requires it"""
    _global_limiter.wait_if_needed(api_name)


def record_request(api_name: str, success: bool = True):
    """Record request completion"""
    _global_limiter.record_request(api_name, success)


def trigger_backoff(api_name: str, error_code: Optional[int] = None):
    """Trigger exponential backoff"""
    _global_limiter.trigger_backoff(api_name, error_code)


def rate_limited(api_name: str):
    """Decorator for rate-limited functions"""
    return _global_limiter.decorator(api_name)


# Standalone test
if __name__ == '__main__':
    import requests
    
    print("Testing Rate Limiter...")
    print("=" * 60)
    
    # Configure limiter for test API
    limiter = RateLimiter()
    limiter.configure('test_api', requests_per_minute=5, min_delay_seconds=1.0)
    
    # Simulate requests
    print("\nSimulating 7 rapid requests (limit: 5/min, 1s delay):\n")
    
    for i in range(7):
        start = time.time()
        
        # Check throttle status before request
        should_wait, wait_time = limiter._should_throttle('test_api')
        if should_wait:
            print(f"Request {i+1}: Rate limited! Waiting {wait_time:.1f}s...")
        
        # Wait if needed
        limiter.wait_if_needed('test_api')
        
        # Make request
        elapsed = time.time() - start
        print(f"Request {i+1}: Made after {elapsed:.1f}s delay")
        
        # Record it
        limiter.record_request('test_api', success=True)
    
    print("\n" + "=" * 60)
    print("Stats:", limiter.get_stats('test_api'))
