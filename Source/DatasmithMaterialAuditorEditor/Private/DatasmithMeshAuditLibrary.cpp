#include "DatasmithMeshAuditLibrary.h"

#include "Components/LineBatchComponent.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "DynamicMesh/DynamicMesh3.h"
#include "DynamicMesh/DynamicMeshAABBTree3.h"
#include "Engine/EngineTypes.h"
#include "Engine/StaticMesh.h"
#include "Engine/World.h"
#include "HAL/PlatformTime.h"
#include "MeshDescription.h"
#include "Spatial/FastWinding.h"
#include "StaticMeshAttributes.h"

namespace DatasmithMeshAuditPrivate
{
using namespace UE::Geometry;

static constexpr uint32 TriangleFaceHighlightBatchId = 0x444D4148u; // "DMAH"
static constexpr double ReversedNormalDotThreshold = -0.2;
static constexpr double PerpendicularNormalDotThreshold = 0.2;
static constexpr double WindingInsideThreshold = 0.65;
static constexpr double WindingOutsideThreshold = 0.35;

static bool IsFinite(const FVector& Value)
{
    return FMath::IsFinite(Value.X) && FMath::IsFinite(Value.Y) && FMath::IsFinite(Value.Z);
}

static bool IsFinite(const FVector2f& Value)
{
    return FMath::IsFinite(Value.X) && FMath::IsFinite(Value.Y);
}

static bool IsFiniteColor(const FLinearColor& Color)
{
    return FMath::IsFinite(Color.R) && FMath::IsFinite(Color.G) &&
        FMath::IsFinite(Color.B) && FMath::IsFinite(Color.A);
}

static FVector ComputeMeshDescriptionFaceCross(
    const FVector& Position0,
    const FVector& Position1,
    const FVector& Position2)
{
    // UE MeshDescription and GeometryCore both use Unreal's left-handed
    // convention: Edge2 x Edge1. Do not reverse indices during conversion.
    return FVector::CrossProduct(Position2 - Position0, Position1 - Position0);
}

static void ClearTriangleFaceHighlightBatch(UWorld* World)
{
    if (World == nullptr)
    {
        return;
    }
    constexpr UWorld::ELineBatcherType BatcherTypes[] = {
        UWorld::ELineBatcherType::WorldPersistent,
        UWorld::ELineBatcherType::ForegroundPersistent};
    for (const UWorld::ELineBatcherType BatcherType : BatcherTypes)
    {
        if (ULineBatchComponent* LineBatcher = World->GetLineBatcher(BatcherType))
        {
            LineBatcher->ClearBatch(TriangleFaceHighlightBatchId);
        }
    }
}

static FName IssueCode(const EDatasmithMeshAuditIssueType Type)
{
    switch (Type)
    {
    case EDatasmithMeshAuditIssueType::InvalidNormal: return TEXT("INVALID_NORMAL");
    case EDatasmithMeshAuditIssueType::DegenerateTriangle: return TEXT("DEGENERATE_TRIANGLE");
    case EDatasmithMeshAuditIssueType::LocalWindingConflict: return TEXT("LOCAL_WINDING_CONFLICT");
    case EDatasmithMeshAuditIssueType::VertexNormalReversed: return TEXT("VERTEX_NORMAL_REVERSED");
    case EDatasmithMeshAuditIssueType::ClosedShellInward: return TEXT("CLOSED_SHELL_INWARD");
    case EDatasmithMeshAuditIssueType::NonManifoldTopology: return TEXT("NON_MANIFOLD_TOPOLOGY");
    case EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown: return TEXT("OPEN_MESH_DIRECTION_UNKNOWN");
    case EDatasmithMeshAuditIssueType::ActorNegativeScale: return TEXT("ACTOR_NEGATIVE_SCALE");
    case EDatasmithMeshAuditIssueType::MissingUvChannel: return TEXT("MISSING_UV_CHANNEL");
    case EDatasmithMeshAuditIssueType::InvalidTriangleUv: return TEXT("INVALID_TRIANGLE_UV");
    case EDatasmithMeshAuditIssueType::DegenerateTriangleUv: return TEXT("DEGENERATE_TRIANGLE_UV");
    case EDatasmithMeshAuditIssueType::UvStretch: return TEXT("UV_STRETCH");
    default: return TEXT("UNKNOWN");
    }
}

struct FTriangleData
{
    int32 SourceId = INDEX_NONE;
    int32 VertexIds[3] = {INDEX_NONE, INDEX_NONE, INDEX_NONE};
    FVector Positions[3] = {FVector::ZeroVector, FVector::ZeroVector, FVector::ZeroVector};
    FVector FaceNormal = FVector::ZeroVector;
    double TwiceArea = 0.0;
    double Area = 0.0;
    int32 MaterialSlot = INDEX_NONE;
    bool bDegenerate = true;
};

struct FParityConstraint
{
    int32 Other = INDEX_NONE;
    uint8 XorParity = 0;
};

struct FShellData
{
    TArray<int32> DenseTriangles;
    bool bHasBoundary = false;
    bool bHasNonManifold = false;
    bool bHasInvalidGeometry = false;
    bool bOrientable = true;
    int32 SameDirectionEdgeCount = 0;
    int32 ContradictoryConstraintCount = 0;
    TSet<int32> ParityDense[2];
    double ParityArea[2] = {0.0, 0.0};
    int32 FallbackCandidateSide = INDEX_NONE;
    TSet<int32> LocalCandidateDense;
    EDatasmithMeshAuditSeverity LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Warning;
    FName LocalCandidateDetail = NAME_None;
    FString LocalCandidateDescription;
};

struct FRawFinding
{
    EDatasmithMeshAuditIssueType Type = EDatasmithMeshAuditIssueType::Unknown;
    EDatasmithMeshAuditSeverity Severity = EDatasmithMeshAuditSeverity::Info;
    FName DetailCode = NAME_None;
    FString Description;
    TSet<int32> SourceTriangleIds;
    double MaxMetric = 0.0;
    TMap<int32, TSet<int32>> ElementIdsBySourceTriangle;
    TMap<int32, double> MetricBySourceTriangle;
};

struct FShellWindingData
{
    TUniquePtr<FDynamicMesh3> Mesh;
    TUniquePtr<FDynamicMeshAABBTree3> Tree;
    TUniquePtr<TFastWindingTree<FDynamicMesh3>> Winding;
    FBox Bounds = FBox(ForceInit);
    TArray<FVector3d> InteriorSamples;
    bool bBuilt = false;
    bool bVoteReliable = false;
    bool bContainmentReliable = false;
    bool bNestedOrOverlapping = false;
    bool bCorrectedNormalsPointInward = false;
    int32 ContainmentDepth = 0;
};

static int32 ResolveMaterialSlot(
    const UStaticMesh* StaticMesh,
    const FMeshDescription& MeshDescription,
    const TPolygonGroupAttributesConstRef<FName>& PolygonGroupSlotNames,
    const FTriangleID TriangleId)
{
    const FPolygonGroupID PolygonGroupId = MeshDescription.GetTrianglePolygonGroup(TriangleId);
    if (PolygonGroupSlotNames.IsValid())
    {
        const FName SlotName = PolygonGroupSlotNames[PolygonGroupId];
        int32 Index = StaticMesh->GetMaterialIndexFromImportedMaterialSlotName(SlotName);
        if (Index == INDEX_NONE)
        {
            Index = StaticMesh->GetMaterialIndex(SlotName);
        }
        if (Index != INDEX_NONE)
        {
            return Index;
        }
    }
    const int32 GroupIndex = PolygonGroupId.GetValue();
    return StaticMesh->GetStaticMaterials().IsValidIndex(GroupIndex) ? GroupIndex : INDEX_NONE;
}

static int32 TriangleEdgeDirection(
    const FMeshDescription& MeshDescription,
    const FTriangleID TriangleId,
    const FVertexID EdgeVertex0,
    const FVertexID EdgeVertex1)
{
    const TArrayView<const FVertexID> VertexIds = MeshDescription.GetTriangleVertices(TriangleId);
    if (VertexIds.Num() != 3)
    {
        return 0;
    }
    for (int32 Corner = 0; Corner < 3; ++Corner)
    {
        const FVertexID Current = VertexIds[Corner];
        const FVertexID Next = VertexIds[(Corner + 1) % 3];
        if (Current == EdgeVertex0 && Next == EdgeVertex1) return 1;
        if (Current == EdgeVertex1 && Next == EdgeVertex0) return -1;
    }
    return 0;
}

static FVector AbsFiniteScale(const FVector& Input)
{
    return FVector(
        FMath::IsFinite(Input.X) ? FMath::Abs(Input.X) : 1.0,
        FMath::IsFinite(Input.Y) ? FMath::Abs(Input.Y) : 1.0,
        FMath::IsFinite(Input.Z) ? FMath::Abs(Input.Z) : 1.0);
}

static FVector ApplyScale(const FVector& Point, const FVector& Scale)
{
    return FVector(Point.X * Scale.X, Point.Y * Scale.Y, Point.Z * Scale.Z);
}

static double ComputeStretchRatio(
    const FTriangleData& Triangle,
    const FVector2f& UV0,
    const FVector2f& UV1,
    const FVector2f& UV2,
    const FVector& Scale)
{
    const FVector P0 = ApplyScale(Triangle.Positions[0], Scale);
    const FVector P1 = ApplyScale(Triangle.Positions[1], Scale);
    const FVector P2 = ApplyScale(Triangle.Positions[2], Scale);
    const FVector Edge1 = P1 - P0;
    const FVector Edge2 = P2 - P0;
    const double U1 = UV1.X - UV0.X;
    const double V1 = UV1.Y - UV0.Y;
    const double U2 = UV2.X - UV0.X;
    const double V2 = UV2.Y - UV0.Y;
    const double Determinant = U1 * V2 - U2 * V1;
    if (!FMath::IsFinite(Determinant) || FMath::Abs(Determinant) <= 1.0e-30)
    {
        return 1.0e12;
    }
    const FVector JacobianU = (Edge1 * V2 - Edge2 * V1) / Determinant;
    const FVector JacobianV = (Edge2 * U1 - Edge1 * U2) / Determinant;
    const double A = FVector::DotProduct(JacobianU, JacobianU);
    const double B = FVector::DotProduct(JacobianU, JacobianV);
    const double C = FVector::DotProduct(JacobianV, JacobianV);
    if (!FMath::IsFinite(A) || !FMath::IsFinite(B) || !FMath::IsFinite(C)) return 1.0e12;
    const double Disc = FMath::Sqrt(FMath::Max(0.0, (A - C) * (A - C) + 4.0 * B * B));
    const double LambdaMax = FMath::Max(0.0, 0.5 * (A + C + Disc));
    const double LambdaMin = FMath::Max(0.0, 0.5 * (A + C - Disc));
    if (LambdaMax <= 1.0e-30) return 1.0;
    if (LambdaMin <= FMath::Max(1.0e-30, LambdaMax * 1.0e-24)) return 1.0e12;
    return FMath::Min(1.0e12, FMath::Sqrt(LambdaMax / LambdaMin));
}

static void AddFinding(
    TArray<FRawFinding>& Findings,
    const EDatasmithMeshAuditIssueType Type,
    const EDatasmithMeshAuditSeverity Severity,
    const FName DetailCode,
    const FString& Description,
    const TSet<int32>& SourceTriangleIds,
    const double MaxMetric = 0.0,
    const TMap<int32, TSet<int32>>* ElementIdsBySourceTriangle = nullptr,
    const TMap<int32, double>* MetricBySourceTriangle = nullptr)
{
    if (SourceTriangleIds.IsEmpty()) return;
    FRawFinding& Finding = Findings.AddDefaulted_GetRef();
    Finding.Type = Type;
    Finding.Severity = Severity;
    Finding.DetailCode = DetailCode;
    Finding.Description = Description;
    Finding.SourceTriangleIds = SourceTriangleIds;
    Finding.MaxMetric = MaxMetric;
    if (ElementIdsBySourceTriangle != nullptr)
    {
        Finding.ElementIdsBySourceTriangle = *ElementIdsBySourceTriangle;
    }
    if (MetricBySourceTriangle != nullptr)
    {
        Finding.MetricBySourceTriangle = *MetricBySourceTriangle;
    }
}

static TSet<int32> DenseToSourceSet(
    const TSet<int32>& DenseIds,
    const TArray<FTriangleData>& Triangles)
{
    TSet<int32> Result;
    Result.Reserve(DenseIds.Num());
    for (const int32 DenseId : DenseIds)
    {
        if (Triangles.IsValidIndex(DenseId)) Result.Add(Triangles[DenseId].SourceId);
    }
    return Result;
}

static TSet<int32> DenseArrayToSourceSet(
    const TArray<int32>& DenseIds,
    const TArray<FTriangleData>& Triangles)
{
    TSet<int32> Result;
    Result.Reserve(DenseIds.Num());
    for (const int32 DenseId : DenseIds)
    {
        if (Triangles.IsValidIndex(DenseId)) Result.Add(Triangles[DenseId].SourceId);
    }
    return Result;
}

static bool BoundsContains(const FBox& Outer, const FBox& Inner, const double Tolerance)
{
    return Outer.IsValid && Inner.IsValid &&
        Inner.Min.X >= Outer.Min.X - Tolerance && Inner.Min.Y >= Outer.Min.Y - Tolerance &&
        Inner.Min.Z >= Outer.Min.Z - Tolerance && Inner.Max.X <= Outer.Max.X + Tolerance &&
        Inner.Max.Y <= Outer.Max.Y + Tolerance && Inner.Max.Z <= Outer.Max.Z + Tolerance;
}

static bool BoundsOverlap(const FBox& A, const FBox& B, const double Tolerance)
{
    return A.IsValid && B.IsValid && A.Min.X <= B.Max.X + Tolerance && A.Max.X >= B.Min.X - Tolerance &&
        A.Min.Y <= B.Max.Y + Tolerance && A.Max.Y >= B.Min.Y - Tolerance &&
        A.Min.Z <= B.Max.Z + Tolerance && A.Max.Z >= B.Min.Z - Tolerance;
}

static void EmitFindingRegions(
    const TArray<FRawFinding>& Findings,
    const TArray<FTriangleData>& Triangles,
    const TMap<int32, int32>& SourceToDense,
    const TArray<TArray<int32>>& Adjacency,
    const int32 MaxSamples,
    FDatasmithMeshAuditResult& Result)
{
    int32 NextRegionIndex = 0;
    const int32 SampleLimit = FMath::Clamp(MaxSamples, 0, 512);
    for (const FRawFinding& Finding : Findings)
    {
        TSet<int32> AllowedDense;
        for (const int32 SourceId : Finding.SourceTriangleIds)
        {
            if (const int32* DenseId = SourceToDense.Find(SourceId)) AllowedDense.Add(*DenseId);
        }
        TArray<int32> OrderedSeeds;
        OrderedSeeds.Reserve(AllowedDense.Num());
        for (const int32 DenseId : AllowedDense) OrderedSeeds.Add(DenseId);
        OrderedSeeds.Sort([&Triangles](const int32 A, const int32 B)
        {
            return Triangles[A].SourceId < Triangles[B].SourceId;
        });
        TSet<int32> Visited;
        for (const int32 Seed : OrderedSeeds)
        {
            if (Visited.Contains(Seed)) continue;
            TArray<int32> Stack{Seed};
            Visited.Add(Seed);
            TArray<int32> RegionDense;
            while (!Stack.IsEmpty())
            {
                const int32 Current = Stack.Pop(EAllowShrinking::No);
                RegionDense.Add(Current);
                for (const int32 Neighbor : Adjacency[Current])
                {
                    if (AllowedDense.Contains(Neighbor) && !Visited.Contains(Neighbor))
                    {
                        Visited.Add(Neighbor);
                        Stack.Add(Neighbor);
                    }
                }
            }
            RegionDense.Sort([&Triangles](const int32 A, const int32 B)
            {
                return Triangles[A].SourceId < Triangles[B].SourceId;
            });

            FDatasmithMeshAuditIssue& Issue = Result.Issues.AddDefaulted_GetRef();
            Issue.IssueType = Finding.Type;
            Issue.IssueCode = IssueCode(Finding.Type);
            Issue.DetailCode = Finding.DetailCode;
            Issue.Severity = Finding.Severity;
            Issue.Description = Finding.Description;
            Issue.LODIndex = 0;
            Issue.RegionIndex = NextRegionIndex++;
            Issue.TriangleIdSpace = TEXT("source_mesh_description_lod0");
            Issue.Count = RegionDense.Num();
            double RegionMaxMetric = Finding.MetricBySourceTriangle.IsEmpty()
                ? Finding.MaxMetric : 0.0;

            FBox Bounds(ForceInit);
            uint32 RegionHash = HashCombine(
                GetTypeHash(Issue.IssueCode.ToString()),
                GetTypeHash(Finding.DetailCode.ToString()));
            TSet<int32> RegionElementIds;
            int32 CommonMaterial = INDEX_NONE;
            bool bFirstMaterial = true;
            for (const int32 DenseId : RegionDense)
            {
                const FTriangleData& Triangle = Triangles[DenseId];
                Issue.ErrorTriangleIds.Add(Triangle.SourceId);
                RegionHash = HashCombine(RegionHash, GetTypeHash(Triangle.SourceId));
                for (const FVector& Position : Triangle.Positions)
                {
                    if (IsFinite(Position)) Bounds += Position;
                }
                if (const TSet<int32>* Elements = Finding.ElementIdsBySourceTriangle.Find(Triangle.SourceId))
                {
                    RegionElementIds.Append(*Elements);
                }
                if (const double* Metric = Finding.MetricBySourceTriangle.Find(Triangle.SourceId))
                {
                    if (FMath::IsFinite(*Metric))
                    {
                        RegionMaxMetric = FMath::Max(RegionMaxMetric, *Metric);
                    }
                }
                if (bFirstMaterial)
                {
                    CommonMaterial = Triangle.MaterialSlot;
                    bFirstMaterial = false;
                }
                else if (CommonMaterial != Triangle.MaterialSlot)
                {
                    CommonMaterial = INDEX_NONE;
                }
            }
            Issue.MaterialSlot = CommonMaterial;
            Issue.MaxMetric = RegionMaxMetric;
            if (!RegionElementIds.IsEmpty())
            {
                Issue.Count = RegionElementIds.Num();
            }
            const int32 MinimumSourceId = Issue.ErrorTriangleIds.IsEmpty()
                ? INDEX_NONE : Issue.ErrorTriangleIds[0];
            Issue.RegionId = FString::Printf(
                TEXT("%s_%s_L0_T%d_H%08X"),
                *Issue.IssueCode.ToString(),
                Finding.DetailCode.IsNone() ? TEXT("GENERAL") : *Finding.DetailCode.ToString(),
                MinimumSourceId,
                RegionHash);
            if (Bounds.IsValid)
            {
                Issue.LocalBoundsMin = Bounds.Min;
                Issue.LocalBoundsMax = Bounds.Max;
            }
            const int32 Samples = FMath::Min(SampleLimit, RegionDense.Num());
            Issue.bSamplesTruncated = Samples < RegionDense.Num();
            for (int32 Index = 0; Index < Samples; ++Index)
            {
                const FTriangleData& Triangle = Triangles[RegionDense[Index]];
                Issue.TriangleIds.Add(Triangle.SourceId);
                Issue.SampleVertices.Append(Triangle.Positions, 3);
                Issue.SampleFaceNormals.Add(Triangle.FaceNormal);
            }
        }
    }
}
} // namespace DatasmithMeshAuditPrivate

int32 UDatasmithMeshAuditLibrary::DrawTriangleFaceHighlights(
    USceneComponent* SceneComponent,
    const TArray<FVector>& LocalTriangleVertices,
    const FLinearColor Color,
    const float LifeTime,
    const bool bForeground,
    const bool bClearExisting)
{
    using namespace DatasmithMeshAuditPrivate;
    if (!IsValid(SceneComponent) || !FMath::IsFinite(LifeTime) || !IsFiniteColor(Color) ||
        LocalTriangleVertices.Num() < 3 || LocalTriangleVertices.Num() % 3 != 0 ||
        LocalTriangleVertices.Num() > MAX_int32 / 2)
    {
        return 0;
    }
    UWorld* World = SceneComponent->GetWorld();
    if (World == nullptr) return 0;
    const UWorld::ELineBatcherType BatcherType = bForeground
        ? UWorld::ELineBatcherType::ForegroundPersistent
        : UWorld::ELineBatcherType::WorldPersistent;
    ULineBatchComponent* LineBatcher = World->GetLineBatcher(BatcherType);
    if (LineBatcher == nullptr) return 0;

    const FTransform Transform = SceneComponent->GetComponentTransform();
    TArray<FVector> WorldVertices;
    WorldVertices.Reserve(LocalTriangleVertices.Num());
    int32 FaceCount = 0;
    for (int32 Offset = 0; Offset + 2 < LocalTriangleVertices.Num(); Offset += 3)
    {
        const FVector P0 = Transform.TransformPosition(LocalTriangleVertices[Offset]);
        const FVector P1 = Transform.TransformPosition(LocalTriangleVertices[Offset + 1]);
        const FVector P2 = Transform.TransformPosition(LocalTriangleVertices[Offset + 2]);
        if (!IsFinite(P0) || !IsFinite(P1) || !IsFinite(P2)) continue;
        WorldVertices.Add(P0); WorldVertices.Add(P1); WorldVertices.Add(P2);
        ++FaceCount;
    }
    if (FaceCount == 0) return 0;
    if (bClearExisting) ClearTriangleFaceHighlightBatch(World);
    TArray<FBatchedLine> OutlineLines;
    OutlineLines.Reserve(FaceCount * 3);
    const FLinearColor OutlineColor(Color.R, Color.G, Color.B, 1.0f);
    const uint8 DepthPriority = static_cast<uint8>(bForeground ? SDPG_Foreground : SDPG_World);
    for (int32 VertexOffset = 0; VertexOffset + 2 < WorldVertices.Num(); VertexOffset += 3)
    {
        const FVector& P0 = WorldVertices[VertexOffset];
        const FVector& P1 = WorldVertices[VertexOffset + 1];
        const FVector& P2 = WorldVertices[VertexOffset + 2];
        OutlineLines.Emplace(P0, P1, OutlineColor, LifeTime, 2.0f, DepthPriority, TriangleFaceHighlightBatchId);
        OutlineLines.Emplace(P1, P2, OutlineColor, LifeTime, 2.0f, DepthPriority, TriangleFaceHighlightBatchId);
        OutlineLines.Emplace(P2, P0, OutlineColor, LifeTime, 2.0f, DepthPriority, TriangleFaceHighlightBatchId);
    }
    LineBatcher->DrawLines(OutlineLines);
    return FaceCount;
}

void UDatasmithMeshAuditLibrary::ClearTriangleFaceHighlights(USceneComponent* SceneComponent)
{
    using namespace DatasmithMeshAuditPrivate;
    if (IsValid(SceneComponent)) ClearTriangleFaceHighlightBatch(SceneComponent->GetWorld());
}

int32 UDatasmithMeshAuditLibrary::DrawStaticMeshTriangleHighlights(
    UStaticMeshComponent* StaticMeshComponent,
    const TArray<int32>& SourceTriangleIds,
    const FLinearColor Color,
    const float LifeTime,
    const bool bForeground,
    const bool bClearExisting,
    const int32 MaxFaces)
{
    using namespace DatasmithMeshAuditPrivate;
    constexpr int32 HardFaceLimit = 100000;
    if (!IsValid(StaticMeshComponent) || MaxFaces <= 0 || SourceTriangleIds.IsEmpty()) return 0;
    UStaticMesh* StaticMesh = StaticMeshComponent->GetStaticMesh();
    if (StaticMesh == nullptr) return 0;

#if !WITH_EDITORONLY_DATA
    return 0;
#else
    FMeshDescription MeshDescription;
    if (!StaticMesh->CloneMeshDescription(0, MeshDescription)) return 0;
    const FStaticMeshConstAttributes Attributes(MeshDescription);
    const TVertexAttributesConstRef<FVector3f> VertexPositions = Attributes.GetVertexPositions();
    if (!VertexPositions.IsValid()) return 0;

    FVector BuildScale = FVector::OneVector;
    if (StaticMesh->GetNumSourceModels() > 0)
    {
        BuildScale = FVector(StaticMesh->GetSourceModel(0).BuildSettings.BuildScale3D);
        if (!IsFinite(BuildScale)) BuildScale = FVector::OneVector;
    }
    const int32 FaceLimit = FMath::Min(MaxFaces, HardFaceLimit);
    TSet<int32> SeenIds;
    TArray<FVector> LocalVertices;
    LocalVertices.Reserve(FMath::Min(SourceTriangleIds.Num(), FaceLimit) * 3);
    for (const int32 SourceId : SourceTriangleIds)
    {
        if (SeenIds.Contains(SourceId) || LocalVertices.Num() / 3 >= FaceLimit) continue;
        SeenIds.Add(SourceId);
        const FTriangleID TriangleId(SourceId);
        if (!MeshDescription.Triangles().IsValid(TriangleId)) continue;
        const TArrayView<const FVertexID> VertexIds = MeshDescription.GetTriangleVertices(TriangleId);
        if (VertexIds.Num() != 3) continue;
        FVector Positions[3];
        bool bValid = true;
        for (int32 Corner = 0; Corner < 3; ++Corner)
        {
            Positions[Corner] = ApplyScale(FVector(VertexPositions[VertexIds[Corner]]), BuildScale);
            bValid &= IsFinite(Positions[Corner]);
        }
        if (!bValid) continue;
        LocalVertices.Append(Positions, 3);
    }
    return DrawTriangleFaceHighlights(
        StaticMeshComponent,
        LocalVertices,
        Color,
        LifeTime,
        bForeground,
        bClearExisting);
#endif
}

FDatasmithComponentTransformAuditResult UDatasmithMeshAuditLibrary::AnalyzeSceneComponentTransform(
    USceneComponent* SceneComponent)
{
    FDatasmithComponentTransformAuditResult Result;
    if (!IsValid(SceneComponent))
    {
        Result.Error = TEXT("SceneComponent is null or invalid.");
        return Result;
    }
    Result.Determinant = SceneComponent->GetComponentTransform().ToMatrixWithScale().Determinant();
    if (!FMath::IsFinite(Result.Determinant))
    {
        Result.Error = TEXT("The complete component-to-world transform determinant is not finite.");
        return Result;
    }
    Result.bOrientationReversing = Result.Determinant < 0.0;
    Result.Success = true;
    return Result;
}

FDatasmithMeshAuditResult UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
    UStaticMesh* StaticMesh,
    FDatasmithMeshAuditOptions Options)
{
    using namespace DatasmithMeshAuditPrivate;
    FDatasmithMeshAuditResult Result;
    const double StartSeconds = FPlatformTime::Seconds();
    auto Finish = [&Result, StartSeconds]()
    {
        Result.AnalysisMilliseconds = (FPlatformTime::Seconds() - StartSeconds) * 1000.0;
        return Result;
    };
    if (StaticMesh == nullptr)
    {
        Result.Error = TEXT("StaticMesh is null.");
        return Finish();
    }
    Result.MeshPath = StaticMesh->GetPathName();
    Result.NormalDataSource = TEXT("source_mesh_description_lod0");

#if !WITH_EDITORONLY_DATA
    Result.Error = TEXT("Source MeshDescription data is unavailable outside an editor build.");
    return Finish();
#else
    FMeshDescription MeshDescription;
    if (!StaticMesh->CloneMeshDescription(0, MeshDescription))
    {
        Result.Error = TEXT("Source LOD0 MeshDescription is unavailable.");
        return Finish();
    }
    Result.TriangleCount = MeshDescription.Triangles().Num();
    if (Result.TriangleCount <= 0)
    {
        Result.Error = TEXT("Source LOD0 contains no triangles.");
        return Finish();
    }

    Options.GeometricAreaEpsilon = FMath::Max(0.0, Options.GeometricAreaEpsilon);
    Options.NormalZeroTolerance = FMath::Max(0.0, Options.NormalZeroTolerance);
    Options.NormalUnitTolerance = FMath::Max(0.0, Options.NormalUnitTolerance);
    Options.UVAreaEpsilon = FMath::Max(0.0, Options.UVAreaEpsilon);
    Options.UVStretchWarningThreshold = FMath::Max(1.0, Options.UVStretchWarningThreshold);
    Options.UVStretchErrorThreshold = FMath::Max(
        Options.UVStretchWarningThreshold, Options.UVStretchErrorThreshold);

    const FStaticMeshConstAttributes Attributes(MeshDescription);
    const TVertexAttributesConstRef<FVector3f> VertexPositions = Attributes.GetVertexPositions();
    if (!VertexPositions.IsValid())
    {
        Result.Error = TEXT("Source LOD0 has no vertex position attribute.");
        return Finish();
    }
    const TVertexInstanceAttributesConstRef<FVector3f> VertexNormals =
        Attributes.GetVertexInstanceNormals();
    const TVertexInstanceAttributesConstRef<FVector2f> VertexUVs =
        Attributes.GetVertexInstanceUVs();
    Result.UVChannelCount = VertexUVs.IsValid() ? VertexUVs.GetNumChannels() : 0;
    const bool bHasUVChannel = VertexUVs.IsValid() && Options.UVChannel >= 0 &&
        Options.UVChannel < Result.UVChannelCount;
    const TPolygonGroupAttributesConstRef<FName> SlotNames =
        Attributes.GetPolygonGroupMaterialSlotNames();

    FVector BuildScale = FVector::OneVector;
    bool bRecomputeNormals = false;
    if (StaticMesh->GetNumSourceModels() > 0)
    {
        BuildScale = AbsFiniteScale(FVector(StaticMesh->GetSourceModel(0).BuildSettings.BuildScale3D));
        bRecomputeNormals = StaticMesh->GetSourceModel(0).BuildSettings.bRecomputeNormals;
    }
    if (bRecomputeNormals)
    {
        Result.NormalDataSource =
            TEXT("source_mesh_description_lod0;vi_direction_skipped_build_recomputes");
    }
    const FVector UVScale = AbsFiniteScale(Options.WorldScale) * BuildScale;

    TArray<FTriangleData> Triangles;
    TMap<int32, int32> SourceToDense;
    Triangles.Reserve(Result.TriangleCount);
    for (const FTriangleID TriangleId : MeshDescription.Triangles().GetElementIDs())
    {
        const TArrayView<const FVertexID> VertexIds = MeshDescription.GetTriangleVertices(TriangleId);
        if (VertexIds.Num() != 3) continue;
        const int32 DenseId = Triangles.Num();
        FTriangleData& Triangle = Triangles.AddDefaulted_GetRef();
        Triangle.SourceId = TriangleId.GetValue();
        SourceToDense.Add(Triangle.SourceId, DenseId);
        for (int32 Corner = 0; Corner < 3; ++Corner)
        {
            Triangle.VertexIds[Corner] = VertexIds[Corner].GetValue();
            Triangle.Positions[Corner] = FVector(VertexPositions[VertexIds[Corner]]);
        }
        const FVector Cross = ComputeMeshDescriptionFaceCross(
            Triangle.Positions[0], Triangle.Positions[1], Triangle.Positions[2]);
        const bool bFiniteGeometry = IsFinite(Triangle.Positions[0]) &&
            IsFinite(Triangle.Positions[1]) && IsFinite(Triangle.Positions[2]) &&
            IsFinite(Cross);
        const double RawTwiceArea = bFiniteGeometry ? Cross.Length() : 0.0;
        Triangle.bDegenerate = !bFiniteGeometry || !FMath::IsFinite(RawTwiceArea) ||
            RawTwiceArea <= Options.GeometricAreaEpsilon;
        // Invalid or degenerate geometry must never leak NaN into shell-area
        // ranking, vote offsets, report metrics, or deterministic sorting.
        Triangle.TwiceArea = Triangle.bDegenerate ? 0.0 : RawTwiceArea;
        Triangle.Area = Triangle.bDegenerate ? 0.0 : RawTwiceArea * 0.5;
        Triangle.FaceNormal = Triangle.bDegenerate
            ? FVector::ZeroVector : Cross / RawTwiceArea;
        Triangle.MaterialSlot = ResolveMaterialSlot(
            StaticMesh, MeshDescription, SlotNames, TriangleId);
    }
    Result.TriangleCount = Triangles.Num();

    TArray<TArray<int32>> Adjacency;
    TArray<TArray<FParityConstraint>> Constraints;
    Adjacency.SetNum(Triangles.Num());
    Constraints.SetNum(Triangles.Num());
    TSet<int32> BoundaryDense;
    TSet<int32> NonManifoldDense;
    TMap<int32, TSet<int32>> NonManifoldEdgeIdsBySourceTriangle;
    TMap<int32, int32> SameDirectionEdgesByDense;
    int32 NonManifoldEdgeCount = 0;

    for (const FEdgeID EdgeId : MeshDescription.Edges().GetElementIDs())
    {
        const TArrayView<const FTriangleID> Connected =
            MeshDescription.GetEdgeConnectedTriangleIDs(EdgeId);
        TArray<int32> DenseConnected;
        for (const FTriangleID ConnectedId : Connected)
        {
            if (const int32* DenseId = SourceToDense.Find(ConnectedId.GetValue()))
            {
                DenseConnected.AddUnique(*DenseId);
            }
        }
        for (int32 A = 0; A < DenseConnected.Num(); ++A)
        {
            for (int32 B = A + 1; B < DenseConnected.Num(); ++B)
            {
                Adjacency[DenseConnected[A]].AddUnique(DenseConnected[B]);
                Adjacency[DenseConnected[B]].AddUnique(DenseConnected[A]);
            }
        }
        if (DenseConnected.Num() == 1)
        {
            BoundaryDense.Add(DenseConnected[0]);
        }
        else if (DenseConnected.Num() > 2)
        {
            ++NonManifoldEdgeCount;
            for (const int32 DenseId : DenseConnected)
            {
                NonManifoldDense.Add(DenseId);
                NonManifoldEdgeIdsBySourceTriangle
                    .FindOrAdd(Triangles[DenseId].SourceId)
                    .Add(EdgeId.GetValue());
            }
        }
        else if (DenseConnected.Num() == 2)
        {
            const TArrayView<const FVertexID> EdgeVertices = MeshDescription.GetEdgeVertices(EdgeId);
            if (EdgeVertices.Num() == 2)
            {
                const int32 Dir0 = TriangleEdgeDirection(
                    MeshDescription,
                    FTriangleID(Triangles[DenseConnected[0]].SourceId),
                    EdgeVertices[0], EdgeVertices[1]);
                const int32 Dir1 = TriangleEdgeDirection(
                    MeshDescription,
                    FTriangleID(Triangles[DenseConnected[1]].SourceId),
                    EdgeVertices[0], EdgeVertices[1]);
                if (Dir0 != 0 && Dir1 != 0)
                {
                    const uint8 XorParity = Dir0 == Dir1 ? 1 : 0;
                    Constraints[DenseConnected[0]].Add({DenseConnected[1], XorParity});
                    Constraints[DenseConnected[1]].Add({DenseConnected[0], XorParity});
                    if (XorParity != 0)
                    {
                        SameDirectionEdgesByDense.FindOrAdd(DenseConnected[0])++;
                        SameDirectionEdgesByDense.FindOrAdd(DenseConnected[1])++;
                    }
                }
            }
        }
    }

    TArray<FShellData> Shells;
    TArray<int32> DenseToShell;
    DenseToShell.Init(INDEX_NONE, Triangles.Num());
    for (int32 Seed = 0; Seed < Triangles.Num(); ++Seed)
    {
        if (DenseToShell[Seed] != INDEX_NONE) continue;
        const int32 ShellIndex = Shells.Num();
        FShellData& Shell = Shells.AddDefaulted_GetRef();
        TArray<int32> Stack{Seed};
        DenseToShell[Seed] = ShellIndex;
        while (!Stack.IsEmpty())
        {
            const int32 Current = Stack.Pop(EAllowShrinking::No);
            Shell.DenseTriangles.Add(Current);
            Shell.bHasBoundary |= BoundaryDense.Contains(Current);
            Shell.bHasNonManifold |= NonManifoldDense.Contains(Current);
            Shell.bHasInvalidGeometry |= Triangles[Current].bDegenerate;
            Shell.SameDirectionEdgeCount += SameDirectionEdgesByDense.FindRef(Current);
            for (const int32 Neighbor : Adjacency[Current])
            {
                if (DenseToShell[Neighbor] == INDEX_NONE)
                {
                    DenseToShell[Neighbor] = ShellIndex;
                    Stack.Add(Neighbor);
                }
            }
        }
        Shell.SameDirectionEdgeCount /= 2;
    }

    TArray<int8> Parity;
    Parity.Init(-1, Triangles.Num());
    for (FShellData& Shell : Shells)
    {
        TSet<int32> ContradictionDense;
        for (const int32 Seed : Shell.DenseTriangles)
        {
            if (Parity[Seed] != -1) continue;
            Parity[Seed] = 0;
            TArray<int32> Stack{Seed};
            while (!Stack.IsEmpty())
            {
                const int32 Current = Stack.Pop(EAllowShrinking::No);
                for (const FParityConstraint& Constraint : Constraints[Current])
                {
                    const int8 Expected = Parity[Current] ^ Constraint.XorParity;
                    if (Parity[Constraint.Other] == -1)
                    {
                        Parity[Constraint.Other] = Expected;
                        Stack.Add(Constraint.Other);
                    }
                    else if (Parity[Constraint.Other] != Expected)
                    {
                        Shell.bOrientable = false;
                        ++Shell.ContradictoryConstraintCount;
                        ContradictionDense.Add(Current);
                        ContradictionDense.Add(Constraint.Other);
                    }
                }
            }
        }
        Shell.ContradictoryConstraintCount /= 2;
        if (!Shell.bHasNonManifold)
        {
            int32 Counts[2] = {0, 0};
            for (const int32 DenseId : Shell.DenseTriangles)
            {
                const int32 Side = Parity[DenseId] > 0 ? 1 : 0;
                Shell.ParityDense[Side].Add(DenseId);
                Shell.ParityArea[Side] += Triangles[DenseId].Area;
                Counts[Side]++;
            }
            if (Counts[0] > 0 && Counts[1] > 0)
            {
                if (!Shell.bHasInvalidGeometry)
                {
                    const double AreaTolerance =
                        FMath::Max(Shell.ParityArea[0], Shell.ParityArea[1]) * 1.0e-9;
                    if (Shell.ParityArea[1] + AreaTolerance < Shell.ParityArea[0])
                        Shell.FallbackCandidateSide = 1;
                    else if (Shell.ParityArea[0] + AreaTolerance < Shell.ParityArea[1])
                        Shell.FallbackCandidateSide = 0;
                    else if (Counts[1] < Counts[0]) Shell.FallbackCandidateSide = 1;
                    else if (Counts[0] < Counts[1]) Shell.FallbackCandidateSide = 0;
                    else Shell.FallbackCandidateSide = 1;
                }
                else
                {
                    // Geometry with non-finite/zero-area faces has no reliable
                    // area ordering. Keep a deterministic parity candidate for
                    // highlighting only; the finding remains a warning.
                    Shell.FallbackCandidateSide = Counts[1] < Counts[0] ? 1 :
                        (Counts[0] < Counts[1] ? 0 : 1);
                }
            }
            if (!Shell.bOrientable)
            {
                Shell.LocalCandidateDense.Append(ContradictionDense);
                Shell.LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Warning;
                Shell.LocalCandidateDetail = TEXT("NON_ORIENTABLE_PARITY_CONTRADICTION");
                Shell.LocalCandidateDescription = FString::Printf(
                    TEXT("The full-shell winding parity propagation found %d contradictory constraints. The reported connected faces are candidates, not a guaranteed absolute side."),
                    Shell.ContradictoryConstraintCount);
            }
        }
    }

    TArray<FRawFinding> Findings;
    TSet<int32> DegenerateSource;
    TSet<int32> NormalMissingSource;
    TSet<int32> NormalNonFiniteSource;
    TSet<int32> NormalZeroSource;
    TSet<int32> NormalNonUnitSource;
    TSet<int32> NormalReversedSource;
    TSet<int32> NormalPerpendicularSource;
    TMap<int32, TSet<int32>> NormalMissingVertexInstances;
    TMap<int32, TSet<int32>> NormalNonFiniteVertexInstances;
    TMap<int32, TSet<int32>> NormalZeroVertexInstances;
    TMap<int32, TSet<int32>> NormalNonUnitVertexInstances;
    TMap<int32, double> NormalNonUnitMetricBySourceTriangle;
    TMap<int32, double> NormalReversedMetricBySourceTriangle;
    TMap<int32, double> NormalPerpendicularMetricBySourceTriangle;
    TSet<int32> UVInvalidSource;
    TSet<int32> UVDegenerateSource;
    TSet<int32> UVStretchWarningSource;
    TSet<int32> UVStretchErrorSource;
    double MaxUVStretchWarning = 0.0;
    double MaxUVStretchError = 0.0;

    for (int32 DenseId = 0; DenseId < Triangles.Num(); ++DenseId)
    {
        const FTriangleData& Triangle = Triangles[DenseId];
        const FTriangleID TriangleId(Triangle.SourceId);
        if (Triangle.bDegenerate) DegenerateSource.Add(Triangle.SourceId);
        const TArrayView<const FVertexInstanceID> VertexInstances =
            MeshDescription.GetTriangleVertexInstances(TriangleId);
        if (!VertexNormals.IsValid())
        {
            NormalMissingSource.Add(Triangle.SourceId);
            TSet<int32>& MissingIds =
                NormalMissingVertexInstances.FindOrAdd(Triangle.SourceId);
            for (const FVertexInstanceID VertexInstance : VertexInstances)
            {
                MissingIds.Add(VertexInstance.GetValue());
            }
        }
        bool bTriangleReversed = false;
        bool bTrianglePerpendicular = false;
        if (VertexNormals.IsValid() && VertexInstances.Num() == 3)
        {
            for (const FVertexInstanceID VertexInstance : VertexInstances)
            {
                const FVector Normal(VertexNormals[VertexInstance]);
                if (!IsFinite(Normal))
                {
                    NormalNonFiniteSource.Add(Triangle.SourceId);
                    NormalNonFiniteVertexInstances
                        .FindOrAdd(Triangle.SourceId)
                        .Add(VertexInstance.GetValue());
                    continue;
                }
                const double Length = Normal.Length();
                if (!FMath::IsFinite(Length) || Length <= Options.NormalZeroTolerance)
                {
                    NormalZeroSource.Add(Triangle.SourceId);
                    NormalZeroVertexInstances
                        .FindOrAdd(Triangle.SourceId)
                        .Add(VertexInstance.GetValue());
                    continue;
                }
                const double UnitDeviation = FMath::Abs(Length - 1.0);
                if (Options.bReportNonUnitNormals && UnitDeviation > Options.NormalUnitTolerance)
                {
                    NormalNonUnitSource.Add(Triangle.SourceId);
                    NormalNonUnitVertexInstances
                        .FindOrAdd(Triangle.SourceId)
                        .Add(VertexInstance.GetValue());
                    double& TriangleMetric =
                        NormalNonUnitMetricBySourceTriangle.FindOrAdd(Triangle.SourceId);
                    TriangleMetric = FMath::Max(TriangleMetric, UnitDeviation);
                }
                if (Options.bReportNormalDirectionMismatch && !bRecomputeNormals && !Triangle.bDegenerate)
                {
                    const double Dot = FVector::DotProduct(Normal / Length, Triangle.FaceNormal);
                    if (Dot < ReversedNormalDotThreshold)
                    {
                        bTriangleReversed = true;
                        double& TriangleMetric =
                            NormalReversedMetricBySourceTriangle.FindOrAdd(Triangle.SourceId);
                        TriangleMetric = FMath::Max(TriangleMetric, 1.0 - Dot);
                    }
                    else if (Options.bReportAmbiguousNormals &&
                        FMath::Abs(Dot) <= PerpendicularNormalDotThreshold)
                    {
                        bTrianglePerpendicular = true;
                        double& TriangleMetric =
                            NormalPerpendicularMetricBySourceTriangle.FindOrAdd(Triangle.SourceId);
                        TriangleMetric = FMath::Max(TriangleMetric, 1.0 - FMath::Abs(Dot));
                    }
                }
            }
        }
        if (bTriangleReversed) NormalReversedSource.Add(Triangle.SourceId);
        else if (bTrianglePerpendicular) NormalPerpendicularSource.Add(Triangle.SourceId);

        if (VertexInstances.Num() != 3) continue;
        if (!bHasUVChannel) continue;
        FVector2f UV[3];
        bool bFiniteUV = true;
        for (int32 Corner = 0; Corner < 3; ++Corner)
        {
            UV[Corner] = VertexUVs.Get(VertexInstances[Corner], Options.UVChannel);
            bFiniteUV &= IsFinite(UV[Corner]);
        }
        if (!bFiniteUV)
        {
            UVInvalidSource.Add(Triangle.SourceId);
            continue;
        }
        const FVector2f E1 = UV[1] - UV[0];
        const FVector2f E2 = UV[2] - UV[0];
        const double TwiceUVArea = FMath::Abs(
            static_cast<double>(E1.X) * E2.Y - static_cast<double>(E1.Y) * E2.X);
        if (!FMath::IsFinite(TwiceUVArea) || TwiceUVArea <= Options.UVAreaEpsilon)
        {
            UVDegenerateSource.Add(Triangle.SourceId);
        }
        else if (!Triangle.bDegenerate)
        {
            const double Stretch = ComputeStretchRatio(Triangle, UV[0], UV[1], UV[2], UVScale);
            if (Stretch >= Options.UVStretchErrorThreshold)
            {
                UVStretchErrorSource.Add(Triangle.SourceId);
                MaxUVStretchError = FMath::Max(MaxUVStretchError, Stretch);
            }
            else if (Stretch >= Options.UVStretchWarningThreshold)
            {
                UVStretchWarningSource.Add(Triangle.SourceId);
                MaxUVStretchWarning = FMath::Max(MaxUVStretchWarning, Stretch);
            }
        }
    }

    AddFinding(Findings, EDatasmithMeshAuditIssueType::DegenerateTriangle,
        EDatasmithMeshAuditSeverity::Error, TEXT("GEOMETRY_ZERO_OR_NONFINITE_AREA"),
        TEXT("Source LOD0 contains a repeated, collinear, non-finite, or near-zero-area triangle."),
        DegenerateSource);
    const FString RecomputeNormalsNote = bRecomputeNormals
        ? TEXT(" The mesh build is configured to recompute normals: this remains a source-data health warning, but it is not used to declare a viewport normal error; source VI direction findings are intentionally skipped.")
        : FString();
    const EDatasmithMeshAuditSeverity InvalidSourceNormalSeverity = bRecomputeNormals
        ? EDatasmithMeshAuditSeverity::Warning
        : EDatasmithMeshAuditSeverity::Error;
    AddFinding(Findings, EDatasmithMeshAuditIssueType::InvalidNormal,
        InvalidSourceNormalSeverity, TEXT("NORMAL_ATTRIBUTE_MISSING"),
        TEXT("Source LOD0 has no vertex-instance normal attribute.") + RecomputeNormalsNote,
        NormalMissingSource, 0.0, &NormalMissingVertexInstances);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::InvalidNormal,
        InvalidSourceNormalSeverity, TEXT("NORMAL_NON_FINITE"),
        TEXT("A Source LOD0 vertex-instance normal contains NaN or infinity.") + RecomputeNormalsNote,
        NormalNonFiniteSource, 0.0, &NormalNonFiniteVertexInstances);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::InvalidNormal,
        InvalidSourceNormalSeverity, TEXT("NORMAL_ZERO_LENGTH"),
        TEXT("A Source LOD0 vertex-instance normal is zero or below the configured zero tolerance.") + RecomputeNormalsNote,
        NormalZeroSource, 0.0, &NormalZeroVertexInstances);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::InvalidNormal,
        EDatasmithMeshAuditSeverity::Warning, TEXT("NORMAL_NON_UNIT"),
        TEXT("A Source LOD0 vertex-instance normal is not unit length; this is an InvalidNormal detail, not a separate issue type.") + RecomputeNormalsNote,
        NormalNonUnitSource, 0.0, &NormalNonUnitVertexInstances,
        &NormalNonUnitMetricBySourceTriangle);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::VertexNormalReversed,
        EDatasmithMeshAuditSeverity::Error, TEXT("VI_NORMAL_DOT_BELOW_MINUS_0_2"),
        TEXT("At least one valid vertex-instance normal points clearly behind the geometric face (dot < -0.2)."),
        NormalReversedSource, 0.0, nullptr, &NormalReversedMetricBySourceTriangle);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::VertexNormalReversed,
        EDatasmithMeshAuditSeverity::Warning, TEXT("VI_NORMAL_NEAR_PERPENDICULAR"),
        TEXT("At least one valid vertex-instance normal is near perpendicular to the geometric face (abs(dot) <= 0.2); smooth normals are not required to approach dot 1."),
        NormalPerpendicularSource, 0.0, nullptr, &NormalPerpendicularMetricBySourceTriangle);

    if (!bHasUVChannel)
    {
        TSet<int32> AllTriangles;
        for (const FTriangleData& Triangle : Triangles) AllTriangles.Add(Triangle.SourceId);
        AddFinding(Findings, EDatasmithMeshAuditIssueType::MissingUvChannel,
            EDatasmithMeshAuditSeverity::Error, TEXT("REQUESTED_UV_CHANNEL_MISSING"),
            FString::Printf(TEXT("Requested Source LOD0 UV channel %d does not exist."), Options.UVChannel),
            AllTriangles, Options.UVChannel);
    }
    AddFinding(Findings, EDatasmithMeshAuditIssueType::InvalidTriangleUv,
        EDatasmithMeshAuditSeverity::Error, TEXT("UV_NON_FINITE"),
        TEXT("A Source LOD0 UV triangle contains NaN or infinity."), UVInvalidSource);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::DegenerateTriangleUv,
        EDatasmithMeshAuditSeverity::Error, TEXT("UV_ZERO_AREA"),
        TEXT("A Source LOD0 UV triangle is repeated, collinear, or near zero area."), UVDegenerateSource);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::UvStretch,
        EDatasmithMeshAuditSeverity::Error, TEXT("UV_STRETCH_ERROR"),
        TEXT("UV-to-world anisotropy exceeds the configured error threshold."),
        UVStretchErrorSource, MaxUVStretchError);
    AddFinding(Findings, EDatasmithMeshAuditIssueType::UvStretch,
        EDatasmithMeshAuditSeverity::Warning, TEXT("UV_STRETCH_WARNING"),
        TEXT("UV-to-world anisotropy exceeds the configured warning threshold."),
        UVStretchWarningSource, MaxUVStretchWarning);

    const int32 NonManifoldFindingStart = Findings.Num();
    AddFinding(Findings, EDatasmithMeshAuditIssueType::NonManifoldTopology,
        EDatasmithMeshAuditSeverity::Error, TEXT("EDGE_HAS_MORE_THAN_TWO_TRIANGLES"),
        TEXT("This connected region touches one or more non-manifold shared edges with more than two incident triangles."),
        DenseToSourceSet(NonManifoldDense, Triangles), NonManifoldEdgeCount);
    if (Findings.Num() > NonManifoldFindingStart)
    {
        Findings.Last().ElementIdsBySourceTriangle = MoveTemp(NonManifoldEdgeIdsBySourceTriangle);
    }

    for (const FShellData& Shell : Shells)
    {
        if (Shell.bHasBoundary && !Shell.bHasNonManifold)
        {
            AddFinding(Findings, EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown,
                EDatasmithMeshAuditSeverity::Info, TEXT("OPEN_SHELL_HAS_NO_ABSOLUTE_INSIDE"),
                TEXT("This connected shell has boundary edges. Local parity can be checked, but an open mesh has no reliable whole-shell inward/outward conclusion."),
                DenseArrayToSourceSet(Shell.DenseTriangles, Triangles));
        }
    }

    TArray<FShellWindingData> WindingData;
    WindingData.SetNum(Shells.Num());
    for (int32 ShellIndex = 0; ShellIndex < Shells.Num(); ++ShellIndex)
    {
        const FShellData& Shell = Shells[ShellIndex];
        FShellWindingData& Data = WindingData[ShellIndex];
        if (Shell.bHasBoundary || Shell.bHasNonManifold || !Shell.bOrientable) continue;
        for (const int32 DenseId : Shell.DenseTriangles)
        {
            for (const FVector& Position : Triangles[DenseId].Positions)
            {
                if (IsFinite(Position)) Data.Bounds += Position;
            }
        }
        // Preserve finite bounds for conservative overlap classification, but
        // never construct winding acceleration from invalid geometry.
        if (Shell.bHasInvalidGeometry) continue;
        bool bCanBuild = true;
        Data.Mesh = MakeUnique<FDynamicMesh3>();
        TMap<int32, int32> VertexMap;
        for (const int32 DenseId : Shell.DenseTriangles)
        {
            const FTriangleData& Triangle = Triangles[DenseId];
            if (Triangle.bDegenerate)
            {
                bCanBuild = false;
                break;
            }
            int32 DynamicVertices[3];
            for (int32 Corner = 0; Corner < 3; ++Corner)
            {
                const int32 SourceVertex = Triangle.VertexIds[Corner];
                int32* Existing = VertexMap.Find(SourceVertex);
                if (Existing == nullptr)
                {
                    const int32 DynamicId = Data.Mesh->AppendVertex(FVector3d(Triangle.Positions[Corner]));
                    Existing = &VertexMap.Add(SourceVertex, DynamicId);
                }
                DynamicVertices[Corner] = *Existing;
            }
            // GeometryCore uses the same Edge2 x Edge1 convention as MeshDescription.
            const int32 DynamicTriangle = Parity[DenseId] == 0
                ? Data.Mesh->AppendTriangle(DynamicVertices[0], DynamicVertices[1], DynamicVertices[2])
                : Data.Mesh->AppendTriangle(DynamicVertices[0], DynamicVertices[2], DynamicVertices[1]);
            if (DynamicTriangle < 0)
            {
                bCanBuild = false;
                break;
            }
        }
        if (!bCanBuild || Data.Mesh->TriangleCount() == 0) continue;
        Data.Tree = MakeUnique<FDynamicMeshAABBTree3>(Data.Mesh.Get(), true);
        Data.Winding = MakeUnique<TFastWindingTree<FDynamicMesh3>>(Data.Tree.Get(), true);
        Data.bBuilt = true;

        TArray<int32> Ranked = Shell.DenseTriangles;
        Ranked.Sort([&Triangles](const int32 A, const int32 B)
        {
            return Triangles[A].Area > Triangles[B].Area;
        });
        const double Diagonal = FMath::Max(Data.Bounds.GetSize().Length(), 1.0e-3);
        int32 InwardVotes = 0;
        int32 OutwardVotes = 0;
        for (int32 Rank = 0; Rank < FMath::Min(32, Ranked.Num()); ++Rank)
        {
            const int32 DenseId = Ranked[Rank];
            const FTriangleData& Triangle = Triangles[DenseId];
            const FVector Normal = Parity[DenseId] == 0 ? Triangle.FaceNormal : -Triangle.FaceNormal;
            const FVector Center = (Triangle.Positions[0] + Triangle.Positions[1] + Triangle.Positions[2]) / 3.0;
            const double LongestEdge = FMath::Max3(
                FVector::Distance(Triangle.Positions[0], Triangle.Positions[1]),
                FVector::Distance(Triangle.Positions[1], Triangle.Positions[2]),
                FVector::Distance(Triangle.Positions[2], Triangle.Positions[0]));
            const double Altitude = LongestEdge > UE_DOUBLE_SMALL_NUMBER
                ? Triangle.TwiceArea / LongestEdge : 0.0;
            const double Epsilon = FMath::Clamp(
                FMath::Min(Diagonal * 1.0e-4, Altitude * 0.05),
                Diagonal * 1.0e-7,
                Diagonal * 1.0e-3);
            const FVector3d Plus = FVector3d(Center + Normal * Epsilon);
            const FVector3d Minus = FVector3d(Center - Normal * Epsilon);
            const double PlusWinding = FMath::Abs(Data.Winding->FastWindingNumber(Plus));
            const double MinusWinding = FMath::Abs(Data.Winding->FastWindingNumber(Minus));
            const bool bPlusInside = PlusWinding >= WindingInsideThreshold;
            const bool bPlusOutside = PlusWinding <= WindingOutsideThreshold;
            const bool bMinusInside = MinusWinding >= WindingInsideThreshold;
            const bool bMinusOutside = MinusWinding <= WindingOutsideThreshold;
            if (bPlusInside && bMinusOutside)
            {
                ++InwardVotes;
                if (Data.InteriorSamples.Num() < 3) Data.InteriorSamples.Add(Plus);
            }
            else if (bMinusInside && bPlusOutside)
            {
                ++OutwardVotes;
                if (Data.InteriorSamples.Num() < 3) Data.InteriorSamples.Add(Minus);
            }
        }
        const int32 ReliableVotes = InwardVotes + OutwardVotes;
        const int32 MajorityVotes = FMath::Max(InwardVotes, OutwardVotes);
        Data.bVoteReliable = ReliableVotes >= 3 &&
            static_cast<double>(MajorityVotes) / ReliableVotes >= 0.8 &&
            Data.InteriorSamples.Num() >= 3;
        Data.bContainmentReliable = Data.bVoteReliable;
        Data.bCorrectedNormalsPointInward = InwardVotes > OutwardVotes;
    }

    // Absolute parity anchoring is deliberately deferred until after every
    // closed shell has been checked for containment/intersection ambiguity.
    for (int32 A = 0; A < Shells.Num(); ++A)
    {
        if (!WindingData[A].Bounds.IsValid) continue;
        for (int32 B = A + 1; B < Shells.Num(); ++B)
        {
            if (!WindingData[B].Bounds.IsValid) continue;
            const double Tolerance = FMath::Max(
                WindingData[A].Bounds.GetSize().Length(),
                WindingData[B].Bounds.GetSize().Length()) * 1.0e-7;
            if ((!WindingData[A].bBuilt || !WindingData[B].bBuilt ||
                    !WindingData[A].bVoteReliable || !WindingData[B].bVoteReliable) &&
                BoundsOverlap(WindingData[A].Bounds, WindingData[B].Bounds, Tolerance))
            {
                WindingData[A].bNestedOrOverlapping = true;
                WindingData[B].bNestedOrOverlapping = true;
                continue;
            }
            const bool bBContainsA = BoundsContains(WindingData[B].Bounds, WindingData[A].Bounds, Tolerance);
            const bool bAContainsB = BoundsContains(WindingData[A].Bounds, WindingData[B].Bounds, Tolerance);
            if (!bBContainsA && !bAContainsB)
            {
                if (BoundsOverlap(WindingData[A].Bounds, WindingData[B].Bounds, Tolerance))
                {
                    WindingData[A].bNestedOrOverlapping = true;
                    WindingData[B].bNestedOrOverlapping = true;
                }
                continue;
            }
            auto ClassifySamples = [](const FShellWindingData& Inner, const FShellWindingData& Outer)
            {
                int32 Inside = 0;
                int32 Outside = 0;
                int32 Uncertain = 0;
                for (const FVector3d& Point : Inner.InteriorSamples)
                {
                    const double Winding = FMath::Abs(Outer.Winding->FastWindingNumber(Point));
                    if (Winding >= WindingInsideThreshold) ++Inside;
                    else if (Winding <= WindingOutsideThreshold) ++Outside;
                    else ++Uncertain;
                }
                if (Uncertain > 0 || (Inside > 0 && Outside > 0)) return -1;
                return Inside == Inner.InteriorSamples.Num() ? 1 : 0;
            };
            if (bBContainsA)
            {
                const int32 Classification = ClassifySamples(WindingData[A], WindingData[B]);
                if (Classification == 1)
                {
                    WindingData[A].ContainmentDepth++;
                    WindingData[A].bNestedOrOverlapping = true;
                    WindingData[B].bNestedOrOverlapping = true;
                }
                else if (Classification < 0)
                {
                    WindingData[A].bNestedOrOverlapping = true;
                    WindingData[B].bNestedOrOverlapping = true;
                }
            }
            if (bAContainsB)
            {
                const int32 Classification = ClassifySamples(WindingData[B], WindingData[A]);
                if (Classification == 1)
                {
                    WindingData[B].ContainmentDepth++;
                    WindingData[A].bNestedOrOverlapping = true;
                    WindingData[B].bNestedOrOverlapping = true;
                }
                else if (Classification < 0)
                {
                    WindingData[A].bNestedOrOverlapping = true;
                    WindingData[B].bNestedOrOverlapping = true;
                }
            }
        }
    }

    for (int32 ShellIndex = 0; ShellIndex < Shells.Num(); ++ShellIndex)
    {
        FShellData& Shell = Shells[ShellIndex];
        if (Shell.bHasNonManifold || !Shell.bOrientable ||
            Shell.ParityDense[0].IsEmpty() || Shell.ParityDense[1].IsEmpty())
        {
            continue;
        }

        const FShellWindingData& Data = WindingData[ShellIndex];
        int32 CandidateSide = Shell.FallbackCandidateSide;
        const bool bIsolatedClosedAnchor = !Shell.bHasBoundary &&
            !Shell.bHasInvalidGeometry && Data.bBuilt && Data.bVoteReliable &&
            !Data.bNestedOrOverlapping;
        if (bIsolatedClosedAnchor)
        {
            // Only an isolated closed shell may resolve the global parity
            // complement into an Error. Containment/overlap classification has
            // already completed before this branch is reachable.
            CandidateSide = Data.bCorrectedNormalsPointInward ? 0 : 1;
            Shell.LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Error;
            Shell.LocalCandidateDetail = TEXT("FAST_WINDING_ANCHORED_PARITY_PARTITION");
            Shell.LocalCandidateDescription = FString::Printf(
                TEXT("An isolated closed shell passed containment checks, so fast-winding votes can resolve the parity complement. Parity side %d is the source-face flip region; %d same-direction shared edges participate."),
                CandidateSide,
                Shell.SameDirectionEdgeCount);
        }
        else if (Data.bNestedOrOverlapping)
        {
            Shell.LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Warning;
            Shell.LocalCandidateDetail = TEXT("CONTAINMENT_AMBIGUOUS_PARITY_CANDIDATE");
            Shell.LocalCandidateDescription = FString::Printf(
                TEXT("This shell is nested, contains another shell, overlaps, or may intersect another closed shell. Its two parity solutions remain globally complementary, so the highlighted side is only a deterministic candidate and cannot be promoted to an absolute flip Error; %d same-direction shared edges participate."),
                Shell.SameDirectionEdgeCount);
        }
        else if (Shell.bHasInvalidGeometry)
        {
            Shell.LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Warning;
            Shell.LocalCandidateDetail = TEXT("INVALID_GEOMETRY_PARITY_AMBIGUOUS");
            Shell.LocalCandidateDescription = FString::Printf(
                TEXT("This shell contains non-finite or degenerate triangles, so area ranking and inside/outside anchoring are invalid. The highlighted parity side is a deterministic topology-only candidate, not a reliable smaller-area or absolute front/back conclusion; %d same-direction shared edges participate."),
                Shell.SameDirectionEdgeCount);
        }
        else
        {
            Shell.LocalCandidateSeverity = EDatasmithMeshAuditSeverity::Warning;
            Shell.LocalCandidateDetail = TEXT("MINIMUM_AREA_PARITY_FALLBACK");
            Shell.LocalCandidateDescription = FString::Printf(
                TEXT("Absolute inside/outside anchoring is unavailable for this shell. Because parity has a globally complementary solution, the smaller finite-area partition (deterministic tie-break when equal) is only a local flip candidate, not an absolute front/back conclusion; %d same-direction shared edges participate."),
                Shell.SameDirectionEdgeCount);
        }
        if (CandidateSide == 0 || CandidateSide == 1)
        {
            Shell.LocalCandidateDense.Append(Shell.ParityDense[CandidateSide]);
        }
    }

    for (const FShellData& Shell : Shells)
    {
        if (!Shell.LocalCandidateDense.IsEmpty())
        {
            AddFinding(Findings, EDatasmithMeshAuditIssueType::LocalWindingConflict,
                Shell.LocalCandidateSeverity,
                Shell.LocalCandidateDetail,
                Shell.LocalCandidateDescription,
                DenseToSourceSet(Shell.LocalCandidateDense, Triangles),
                Shell.bOrientable ? Shell.SameDirectionEdgeCount : Shell.ContradictoryConstraintCount);
        }
    }

    for (int32 ShellIndex = 0; ShellIndex < Shells.Num(); ++ShellIndex)
    {
        FShellWindingData& Data = WindingData[ShellIndex];
        const FShellData& Shell = Shells[ShellIndex];
        if (Shell.bHasBoundary || Shell.bHasNonManifold || !Shell.bOrientable) continue;
        ++Result.ClosedShellDirectionCandidateCount;
        FString AbstentionReason;
        if (Shell.bHasInvalidGeometry)
        {
            AbstentionReason = TEXT("closed shell contains non-finite or degenerate triangles; area ranking and winding direction are invalid");
        }
        else if (!Data.bBuilt)
        {
            AbstentionReason = TEXT("closed-shell winding acceleration could not be built");
        }
        else if (Data.bNestedOrOverlapping)
        {
            AbstentionReason = TEXT("shell is contained by, contains, overlaps, or may intersect another closed shell; cavity versus independent embedded object is ambiguous");
        }
        else if (!Data.bVoteReliable)
        {
            AbstentionReason = TEXT("center-plus/minus winding votes were insufficient or inconsistent");
        }
        else if (!Shell.LocalCandidateDense.IsEmpty())
        {
            AbstentionReason = TEXT("source faces contain a local winding parity conflict; whole-shell direction is not a coherent source property");
        }
        if (!AbstentionReason.IsEmpty())
        {
            ++Result.ClosedShellDirectionAbstainedCount;
            Result.DirectionAbstentionReasons.Add(FString::Printf(
                TEXT("Shell %d: %s"), ShellIndex, *AbstentionReason));
            continue;
        }
        ++Result.ClosedShellDirectionEvaluatedCount;
        if (Data.bCorrectedNormalsPointInward)
        {
            AddFinding(Findings, EDatasmithMeshAuditIssueType::ClosedShellInward,
                EDatasmithMeshAuditSeverity::Error, TEXT("ISOLATED_CLOSED_SHELL_INWARD"),
                TEXT("An isolated, orientable closed shell has high-confidence winding votes showing that its coherent face normals point toward its enclosed volume."),
                DenseArrayToSourceSet(Shell.DenseTriangles, Triangles));
        }
    }

    EmitFindingRegions(
        Findings,
        Triangles,
        SourceToDense,
        Adjacency,
        Options.MaxSampleTriangles,
        Result);
    Result.Issues.Sort([](const FDatasmithMeshAuditIssue& A, const FDatasmithMeshAuditIssue& B)
    {
        if (A.Severity != B.Severity)
            return static_cast<uint8>(A.Severity) > static_cast<uint8>(B.Severity);
        if (A.IssueType != B.IssueType)
            return static_cast<uint8>(A.IssueType) < static_cast<uint8>(B.IssueType);
        return A.RegionIndex < B.RegionIndex;
    });
    Result.Success = true;
    return Finish();
#endif
}
