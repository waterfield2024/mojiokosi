"""Optional numerical checks; CI without CUDA skips this class."""
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('torch'), 'PyTorch is not installed')
class ReuseCuda(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        if not torch.cuda.is_available():
            raise unittest.SkipTest('NVIDIA CUDA GPU is not available')
        cls.torch = torch

    def inputs(self, batch=2, length=19, channels=7):
        t = self.torch
        t.manual_seed(123)
        return [t.randn(batch,length,channels), t.rand(batch,length,channels), -t.rand(channels,16)*16,
                t.randn(batch,length,16), t.randn(batch,length,16), t.randn(channels), t.randn(batch,length,channels)]

    def test_cuda_matches_reference_for_partial_warps_and_long_sequences(self):
        from reuse_model.mamba import reference_scan
        from reuse_model.scan_cuda import selective_scan
        for shape in [(2,19,7), (3,161,16), (1,57,256)]:
            with self.subTest(shape=shape):
                args = self.inputs(*shape)
                expected = reference_scan(*args)
                actual = selective_scan(*[a.cuda() for a in args]).cpu()
                self.assertTrue(self.torch.allclose(expected, actual, atol=2e-5, rtol=2e-4))

    def test_scan_uses_current_pytorch_stream(self):
        from reuse_model.mamba import reference_scan
        from reuse_model.scan_cuda import selective_scan
        args = self.inputs()
        expected = reference_scan(*args)
        with self.torch.cuda.stream(self.torch.cuda.Stream()):
            tensors = [a.cuda() for a in args]
            actual = selective_scan(*tensors).cpu()
        self.assertTrue(self.torch.allclose(expected, actual, atol=2e-5, rtol=2e-4))

    def test_invalid_shape_and_noncontiguous_inputs_rejected_before_launch(self):
        from reuse_model.scan_cuda import selective_scan
        args = [a.cuda() for a in self.inputs()]
        args[3] = args[3][:, :-1, :].contiguous()
        with self.assertRaises(ValueError):
            selective_scan(*args)
        args = [a.cuda() for a in self.inputs()]
        args[0] = args[0].transpose(1, 2).contiguous().transpose(1, 2)
        with self.assertRaises(ValueError):
            selective_scan(*args)


if __name__ == '__main__':
    unittest.main()
