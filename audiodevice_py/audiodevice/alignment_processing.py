"""
Alignment processing for audio signals using GCC-PHAT algorithm.
Provides alignment functionality for play+record mode.
"""

import numpy as np

try:
    from base.log_manager import LogManager  # type: ignore

    def _get_logger():
        return LogManager.set_log_handler("alignment")

except Exception:  # pragma: no cover
    import logging

    def _get_logger():
        return logging.getLogger("alignment")


class AlignmentProcessing:
    """
    Handles alignment of recorded signals with stimulus signals using GCC-PHAT.
    """

    @staticmethod
    def gcc_phat(stimulus_signal, recorded_signal):
        """
        Calculate GCC-PHAT cross-correlation and return delay.

        Args:
            stimulus_signal (np.ndarray): Reference stimulus signal
            recorded_signal (np.ndarray): Recorded signal to align

        Returns:
            tuple: (delay_samples, correlation_function, max_shift)
        """
        n = len(recorded_signal) + len(stimulus_signal)
        n_11 = len(stimulus_signal) // 11

        # Compute FFTs
        SIG = np.fft.rfft(recorded_signal, n=n)
        REF = np.fft.rfft(stimulus_signal, n=n)

        # GCC-PHAT: R = SIG * conj(REF) / |SIG * conj(REF)|
        R = SIG * np.conj(REF)

        # Compute correlation
        max_shift = n // 2
        corr_func_r = np.fft.irfft(R)
        corr_func_shifted_r = np.fft.fftshift(corr_func_r)

        # Find peak in correlation
        new_delay_samples_r = 0
        tmp_max = 0
        for i in range(n // 3, n - len(stimulus_signal) // 12, len(stimulus_signal) // 12):
            max_min_diff = max(corr_func_shifted_r[i: i + n_11]) - min(corr_func_shifted_r[i: i + n_11])
            if max_min_diff >= tmp_max:
                tmp_max = max_min_diff
                new_delay_samples_r = i + np.argmax(np.abs(corr_func_shifted_r[i: i + n_11]))

        new_delay_samples_r -= max_shift
        return new_delay_samples_r, corr_func_shifted_r, max_shift

    @staticmethod
    def align_play_and_rec_data_using_gccphat(stimulus_signal, recorded_signal):
        """
        Align recorded signal with stimulus signal using GCC-PHAT and extract aligned portion.

        This method finds where the stimulus appears in the recorded signal and extracts
        only the aligned stimulus portion, matching the old blocking behavior.

        Args:
            stimulus_signal (np.ndarray): Original stimulus signal (without padding)
            recorded_signal (np.ndarray): Recorded signal containing:
                - prepare_frames of silence at start
                - stimulus response
                - prolong_frames of silence at end

        Returns:
            np.ndarray: Aligned audio data with length equal to stimulus_signal length
        """
        logger = _get_logger()

        # Calculate alignment offset using GCC-PHAT
        align_frames, _, _ = AlignmentProcessing.gcc_phat(stimulus_signal, recorded_signal)

        logger.info(f"GCC-PHAT alignment: offset = {align_frames} samples")

        # Extract aligned portion (only the stimulus length)
        # This matches the old behavior: rec_data[align_frames: align_frames + len(stimulus)]
        end_frame = align_frames + len(stimulus_signal)

        # Validate bounds
        if align_frames < 0:
            logger.warning(f"Negative alignment offset {align_frames}, clipping to 0")
            align_frames = 0

        if end_frame > len(recorded_signal):
            logger.warning(f"Alignment end {end_frame} exceeds recording length {len(recorded_signal)}, clipping")
            end_frame = len(recorded_signal)

        # Extract aligned stimulus portion
        aligned_data = recorded_signal[align_frames:end_frame]

        logger.info(f"Extracted aligned data: {len(aligned_data)} samples (expected {len(stimulus_signal)})")

        # If extraction is shorter than expected due to clipping, pad with zeros
        if len(aligned_data) < len(stimulus_signal):
            shortfall = len(stimulus_signal) - len(aligned_data)
            logger.warning(f"Aligned data is {shortfall} samples short, padding with zeros")
            aligned_data = np.pad(aligned_data, (0, shortfall), mode='constant', constant_values=0)

        return aligned_data.astype(np.float32)
