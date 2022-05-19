#include "torch_xla/csrc/ops/all.h"

#include "absl/strings/str_join.h"
#include "torch_xla/csrc/helpers.h"
#include "torch_xla/csrc/lowering_context.h"
#include "torch_xla/csrc/ops/infer_output_shape.h"
#include "torch_xla/csrc/reduction.h"
#include "torch_xla/csrc/tensor_util.h"
#include "torch_xla/csrc/torch_util.h"

namespace torch_xla {
namespace {

xla::Shape NodeOutputShape(const torch::lazy::Value& input,
                           std::vector<int64_t>& dimensions,
                           bool keep_reduced_dimensions) {
  auto lower_for_shape_fn =
      [&](absl::Span<const xla::XlaOp> operands) -> xla::XlaOp {
    return BuildAll(operands[0], dimensions, keep_reduced_dimensions);
  };
  return InferOutputShape({GetXlaShape(input)}, lower_for_shape_fn);
}

}  // namespace

All::All(const torch::lazy::Value& input, std::vector<int64_t> dimensions,
         bool keep_reduced_dimensions)
    : XlaNode(torch::lazy::OpKind(at::aten::all), {input},
              NodeOutputShape(input, dimensions, keep_reduced_dimensions),
              /*num_outputs=*/1,
              torch::lazy::MHash(dimensions, keep_reduced_dimensions)),
      dimensions_(std::move(dimensions)),
      keep_reduced_dimensions_(keep_reduced_dimensions) {}

torch::lazy::NodePtr All::Clone(torch::lazy::OpList operands) const {
  return torch::lazy::MakeNode<All>(operands.at(0), dimensions_,
                                    keep_reduced_dimensions_);
}

XlaOpVector All::Lower(LoweringContext* loctx) const {
  xla::XlaOp input = loctx->GetOutputOp(operand(0));
  return ReturnOp(BuildAll(input, dimensions_, keep_reduced_dimensions_),
                  loctx);
}

std::string All::ToString() const {
  std::stringstream ss;
  ss << XlaNode::ToString() << ", dimensions=("
     << absl::StrJoin(dimensions_, ", ")
     << "), keep_reduced_dimensions=" << keep_reduced_dimensions_;
  return ss.str();
}

}  // namespace torch_xla
