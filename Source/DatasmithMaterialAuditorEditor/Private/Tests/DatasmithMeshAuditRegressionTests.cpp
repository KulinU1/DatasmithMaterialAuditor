#if WITH_DEV_AUTOMATION_TESTS

#include "DatasmithMeshAuditLibrary.h"

#include "Algo/Sort.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Editor.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/World.h"
#include "MeshDescription.h"
#include "Misc/AutomationTest.h"
#include "StaticMeshAttributes.h"

#include <limits>

namespace DatasmithMeshAuditRegressionTests
{
struct FTriangleSpec
{
    int32 A = 0;
    int32 B = 0;
    int32 C = 0;
};

static FVector UnrealFaceNormal(
    const TArray<FVector>& Positions,
    const FTriangleSpec& Triangle)
{
    return FVector::CrossProduct(
        Positions[Triangle.C] - Positions[Triangle.A],
        Positions[Triangle.B] - Positions[Triangle.A]).GetSafeNormal();
}

static FTriangleSpec Reverse(const FTriangleSpec& Triangle)
{
    return {Triangle.A, Triangle.C, Triangle.B};
}

static UStaticMesh* CreateTransientMesh(
    const TArray<FVector>& Positions,
    const TArray<FTriangleSpec>& Triangles,
    const bool bCreateUVChannel = true,
    const bool bReverseNormals = false)
{
    UStaticMesh* StaticMesh = NewObject<UStaticMesh>(GetTransientPackage(), NAME_None, RF_Transient);
    StaticMesh->AddSourceModel();
    StaticMesh->GetSourceModel(0).BuildSettings.bRecomputeNormals = false;
    FMeshDescription* MeshDescription = StaticMesh->CreateMeshDescription(0);
    check(MeshDescription != nullptr);

    FStaticMeshAttributes Attributes(*MeshDescription);
    Attributes.Register();
    TVertexAttributesRef<FVector3f> VertexPositions = Attributes.GetVertexPositions();
    TVertexInstanceAttributesRef<FVector3f> VertexNormals = Attributes.GetVertexInstanceNormals();
    TVertexInstanceAttributesRef<FVector2f> VertexUVs = Attributes.GetVertexInstanceUVs();
    VertexUVs.SetNumChannels(bCreateUVChannel ? 1 : 0);

    TArray<FVertexID> VertexIds;
    for (const FVector& Position : Positions)
    {
        const FVertexID VertexId = MeshDescription->CreateVertex();
        VertexPositions[VertexId] = FVector3f(Position);
        VertexIds.Add(VertexId);
    }

    const FPolygonGroupID PolygonGroupId = MeshDescription->CreatePolygonGroup();
    for (const FTriangleSpec& Triangle : Triangles)
    {
        const FVector FaceNormal = UnrealFaceNormal(Positions, Triangle);
        const FVector Normal = bReverseNormals ? -FaceNormal : FaceNormal;
        TArray<FVertexInstanceID, TInlineAllocator<3>> InstanceIds;
        const int32 Indices[3] = {Triangle.A, Triangle.B, Triangle.C};
        for (int32 CornerIndex = 0; CornerIndex < 3; ++CornerIndex)
        {
            const FVertexInstanceID InstanceId =
                MeshDescription->CreateVertexInstance(VertexIds[Indices[CornerIndex]]);
            VertexNormals[InstanceId] = FVector3f(Normal);
            if (bCreateUVChannel)
            {
                VertexUVs.Set(
                    InstanceId,
                    0,
                    CornerIndex == 0
                        ? FVector2f(0.0f, 0.0f)
                        : (CornerIndex == 1 ? FVector2f(1.0f, 0.0f) : FVector2f(0.0f, 1.0f)));
            }
            InstanceIds.Add(InstanceId);
        }
        MeshDescription->CreateTriangle(PolygonGroupId, InstanceIds);
    }

    StaticMesh->CommitMeshDescription(0);
    return StaticMesh;
}

static const FDatasmithMeshAuditIssue* FindIssue(
    const FDatasmithMeshAuditResult& Result,
    const EDatasmithMeshAuditIssueType Type)
{
    return Result.Issues.FindByPredicate(
        [Type](const FDatasmithMeshAuditIssue& Issue) { return Issue.IssueType == Type; });
}

static bool HasIssue(
    const FDatasmithMeshAuditResult& Result,
    const EDatasmithMeshAuditIssueType Type)
{
    return FindIssue(Result, Type) != nullptr;
}

static TArray<int32> GatherErrorTriangleIds(
    const FDatasmithMeshAuditResult& Result,
    const EDatasmithMeshAuditIssueType Type)
{
    TSet<int32> UniqueIds;
    for (const FDatasmithMeshAuditIssue& Issue : Result.Issues)
    {
        if (Issue.IssueType == Type)
        {
            for (const int32 TriangleId : Issue.ErrorTriangleIds)
            {
                UniqueIds.Add(TriangleId);
            }
        }
    }
    TArray<int32> ResultIds = UniqueIds.Array();
    ResultIds.Sort();
    return ResultIds;
}

static TArray<FVector> TetraPositions()
{
    return {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0),
        FVector(0.0, 0.0, 100.0)};
}

static TArray<FTriangleSpec> OutwardTetraTriangles()
{
    // These are outward under UE's (C-A) x (B-A) front-face convention.
    return {{0, 1, 2}, {0, 2, 3}, {0, 3, 1}, {1, 3, 2}};
}

static void AddQuad(
    TArray<FTriangleSpec>& Triangles,
    const int32 A,
    const int32 B,
    const int32 C,
    const int32 D)
{
    Triangles.Add({A, B, C});
    Triangles.Add({A, C, D});
}

static void AppendOutwardCube(
    TArray<FVector>& Positions,
    TArray<FTriangleSpec>& Triangles,
    const FVector& Center,
    const double HalfExtent)
{
    const int32 Base = Positions.Num();
    Positions.Append({
        Center + FVector(-HalfExtent, -HalfExtent, -HalfExtent),
        Center + FVector(HalfExtent, -HalfExtent, -HalfExtent),
        Center + FVector(HalfExtent, HalfExtent, -HalfExtent),
        Center + FVector(-HalfExtent, HalfExtent, -HalfExtent),
        Center + FVector(-HalfExtent, -HalfExtent, HalfExtent),
        Center + FVector(HalfExtent, -HalfExtent, HalfExtent),
        Center + FVector(HalfExtent, HalfExtent, HalfExtent),
        Center + FVector(-HalfExtent, HalfExtent, HalfExtent)});
    AddQuad(Triangles, Base + 0, Base + 1, Base + 2, Base + 3); // -Z
    AddQuad(Triangles, Base + 4, Base + 7, Base + 6, Base + 5); // +Z
    AddQuad(Triangles, Base + 0, Base + 4, Base + 5, Base + 1); // -Y
    AddQuad(Triangles, Base + 3, Base + 2, Base + 6, Base + 7); // +Y
    AddQuad(Triangles, Base + 0, Base + 3, Base + 7, Base + 4); // -X
    AddQuad(Triangles, Base + 1, Base + 5, Base + 6, Base + 2); // +X
}

static UStaticMesh* CreateOutwardCube()
{
    TArray<FVector> Positions;
    TArray<FTriangleSpec> Triangles;
    AppendOutwardCube(Positions, Triangles, FVector::ZeroVector, 50.0);
    return CreateTransientMesh(Positions, Triangles);
}

static bool IsFiniteVector(const FVector& Value)
{
    return FMath::IsFinite(Value.X) && FMath::IsFinite(Value.Y) &&
        FMath::IsFinite(Value.Z);
}

static FString TriangleIdKey(const FDatasmithMeshAuditIssue& Issue)
{
    TArray<int32> Ids = Issue.ErrorTriangleIds;
    Ids.Sort();
    TArray<FString> Parts;
    for (const int32 Id : Ids) Parts.Add(FString::FromInt(Id));
    return FString::Join(Parts, TEXT(","));
}
} // namespace DatasmithMeshAuditRegressionTests

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditOpenSurfaceTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.OpenSurfaceSemantics",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditOpenSurfaceTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(100.0, 100.0, 0.0),
        FVector(0.0, 100.0, 0.0)};
    const TArray<FTriangleSpec> HealthyTriangles = {{0, 1, 2}, {0, 2, 3}};
    const FDatasmithMeshAuditResult Healthy = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Positions, HealthyTriangles), FDatasmithMeshAuditOptions());
    TestTrue(TEXT("Healthy open quad is analyzable"), Healthy.Success);
    TestEqual(TEXT("Healthy open quad has one informational topology state"), Healthy.Issues.Num(), 1);
    const FDatasmithMeshAuditIssue* OpenIssue =
        FindIssue(Healthy, EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown);
    TestNotNull(TEXT("Healthy open quad reports OPEN_MESH_DIRECTION_UNKNOWN"), OpenIssue);
    if (OpenIssue)
    {
        TestEqual(
            TEXT("Open-mesh direction state is Info"),
            static_cast<uint8>(OpenIssue->Severity),
            static_cast<uint8>(EDatasmithMeshAuditSeverity::Info));
        TestEqual(TEXT("Open quad region contains both faces"), OpenIssue->Count, 2);
    }

    TArray<FTriangleSpec> FlippedTriangles = HealthyTriangles;
    FlippedTriangles[1] = Reverse(FlippedTriangles[1]);
    const FDatasmithMeshAuditResult LocalFlip = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Positions, FlippedTriangles), FDatasmithMeshAuditOptions());
    TestTrue(
        TEXT("One flipped open-quad face produces LOCAL_WINDING_CONFLICT"),
        HasIssue(LocalFlip, EDatasmithMeshAuditIssueType::LocalWindingConflict));
    const FDatasmithMeshAuditIssue* OpenLocalIssue =
        FindIssue(LocalFlip, EDatasmithMeshAuditIssueType::LocalWindingConflict);
    if (OpenLocalIssue)
    {
        TestEqual(
            TEXT("Equal-area complementary sides on an open mesh are review-only"),
            static_cast<uint8>(OpenLocalIssue->Severity),
            static_cast<uint8>(EDatasmithMeshAuditSeverity::Warning));
    }
    TestTrue(
        TEXT("Open equal-area conflict retains OPEN_MESH_DIRECTION_UNKNOWN"),
        HasIssue(LocalFlip, EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown));
    TestFalse(
        TEXT("An open surface is never classified as a globally inward shell"),
        HasIssue(LocalFlip, EDatasmithMeshAuditIssueType::ClosedShellInward));

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditClosedShellOrientationTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.ClosedShellOrientationAndMetadata",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditClosedShellOrientationTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    const TArray<FVector> Positions = TetraPositions();
    const TArray<FTriangleSpec> OutwardTriangles = OutwardTetraTriangles();
    const FDatasmithMeshAuditResult OutwardTetra = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Positions, OutwardTriangles), FDatasmithMeshAuditOptions());
    TestFalse(
        TEXT("Outward tetrahedron is not CLOSED_SHELL_INWARD"),
        HasIssue(OutwardTetra, EDatasmithMeshAuditIssueType::ClosedShellInward));
    TestFalse(
        TEXT("Outward tetrahedron is not open"),
        HasIssue(OutwardTetra, EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown));

    const FDatasmithMeshAuditResult OutwardCube = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateOutwardCube(), FDatasmithMeshAuditOptions());
    TestFalse(
        TEXT("Outward cube is not CLOSED_SHELL_INWARD"),
        HasIssue(OutwardCube, EDatasmithMeshAuditIssueType::ClosedShellInward));
    TestFalse(
        TEXT("Outward cube has no local winding conflict"),
        HasIssue(OutwardCube, EDatasmithMeshAuditIssueType::LocalWindingConflict));

    TArray<FTriangleSpec> InwardTriangles;
    for (const FTriangleSpec& Triangle : OutwardTriangles)
    {
        InwardTriangles.Add(Reverse(Triangle));
    }
    FDatasmithMeshAuditOptions TruncatedPreviewOptions;
    TruncatedPreviewOptions.MaxSampleTriangles = 1;
    const FDatasmithMeshAuditResult Inward = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Positions, InwardTriangles), TruncatedPreviewOptions);
    const FDatasmithMeshAuditIssue* InwardIssue =
        FindIssue(Inward, EDatasmithMeshAuditIssueType::ClosedShellInward);
    TestNotNull(TEXT("Fully reversed closed tetrahedron reports CLOSED_SHELL_INWARD"), InwardIssue);
    if (InwardIssue)
    {
        TArray<int32> CompleteIds = InwardIssue->ErrorTriangleIds;
        CompleteIds.Sort();
        TestEqual(TEXT("Closed-shell result count uses faces"), InwardIssue->Count, 4);
        TestTrue(
            TEXT("Complete region IDs survive preview truncation"),
            CompleteIds == TArray<int32>({0, 1, 2, 3}));
        TestEqual(TEXT("Closed-shell result identifies Source LOD0"), InwardIssue->LODIndex, 0);
        TestEqual(
            TEXT("Closed-shell triangle ID space is explicit"),
            InwardIssue->TriangleIdSpace,
            FString(TEXT("source_mesh_description_lod0")));
        TestFalse(TEXT("Closed-shell result has a stable region ID"), InwardIssue->RegionId.IsEmpty());
        TestTrue(TEXT("Preview truncation is reported"), InwardIssue->bSamplesTruncated);
        TestEqual(TEXT("Only one preview face is retained"), InwardIssue->TriangleIds.Num(), 1);
        TestEqual(TEXT("One preview face has three vertices"), InwardIssue->SampleVertices.Num(), 3);
        TestTrue(TEXT("Region bounds min is complete"), InwardIssue->LocalBoundsMin.Equals(FVector::ZeroVector, 1.0e-4));
        TestTrue(
            TEXT("Region bounds max is complete"),
            InwardIssue->LocalBoundsMax.Equals(FVector(100.0, 100.0, 100.0), 1.0e-4));
    }
    TestEqual(
        TEXT("0.9.5 result declares Source MeshDescription LOD0"),
        Inward.NormalDataSource,
        FString(TEXT("source_mesh_description_lod0")));

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditClosedShellComplementAnchoringTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.ClosedShellComplementAnchoring",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditClosedShellComplementAnchoringTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    // Three of four tetra faces are deliberately reversed. A purely
    // minimum-area/count parity rule would blame the one still-correct face;
    // a closed shell has enough absolute orientation information to anchor
    // the complementary solution and return the actual majority error set.
    TArray<FTriangleSpec> MajorityFlip = OutwardTetraTriangles();
    for (const int32 TriangleIndex : {1, 2, 3})
    {
        MajorityFlip[TriangleIndex] = Reverse(MajorityFlip[TriangleIndex]);
    }
    const FDatasmithMeshAuditResult MajorityResult =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
            CreateTransientMesh(TetraPositions(), MajorityFlip),
            FDatasmithMeshAuditOptions());
    TestTrue(
        TEXT("Closed-shell majority flip is a localized winding finding"),
        HasIssue(MajorityResult, EDatasmithMeshAuditIssueType::LocalWindingConflict));
    TestTrue(
        TEXT("Closed-shell orientation anchors the majority flipped faces, not their small complement"),
        GatherErrorTriangleIds(
            MajorityResult,
            EDatasmithMeshAuditIssueType::LocalWindingConflict) == TArray<int32>({1, 2, 3}));

    // Six equal-area cube triangles are reversed, including Source ID 0.
    // Counts and areas tie exactly, so seed/source ordering must not arbitrarily
    // switch the answer to the complementary six faces.
    TArray<FVector> CubePositions;
    TArray<FTriangleSpec> EqualAreaFlip;
    AppendOutwardCube(CubePositions, EqualAreaFlip, FVector::ZeroVector, 50.0);
    for (int32 TriangleIndex = 0; TriangleIndex < 6; ++TriangleIndex)
    {
        EqualAreaFlip[TriangleIndex] = Reverse(EqualAreaFlip[TriangleIndex]);
    }
    const FDatasmithMeshAuditResult EqualAreaResult =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
            CreateTransientMesh(CubePositions, EqualAreaFlip),
            FDatasmithMeshAuditOptions());
    const TArray<int32> ExpectedEqualAreaIds = {0, 1, 2, 3, 4, 5};
    TestTrue(
        TEXT("Closed-shell absolute orientation anchors an equal-area parity complement"),
        GatherErrorTriangleIds(
            EqualAreaResult,
            EDatasmithMeshAuditIssueType::LocalWindingConflict) == ExpectedEqualAreaIds);

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditNestedIndependentShellTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.NestedOutwardIndependentShells",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditNestedIndependentShellTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    TArray<FVector> Positions;
    TArray<FTriangleSpec> Triangles;
    AppendOutwardCube(Positions, Triangles, FVector::ZeroVector, 100.0);
    AppendOutwardCube(Positions, Triangles, FVector::ZeroVector, 25.0);
    const FDatasmithMeshAuditResult Result = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Positions, Triangles), FDatasmithMeshAuditOptions());
    TestFalse(
        TEXT("A nested but independently outward closed shell is not assumed to be a cavity"),
        HasIssue(Result, EDatasmithMeshAuditIssueType::ClosedShellInward));
    TestFalse(
        TEXT("Nested independent shells retain locally consistent winding"),
        HasIssue(Result, EDatasmithMeshAuditIssueType::LocalWindingConflict));

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditLocalizedTopologyTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.LocalFlipNonManifoldAndDegenerate",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditLocalizedTopologyTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    const TArray<FVector> Tetra = TetraPositions();
    TArray<FTriangleSpec> LocalFlipTriangles = OutwardTetraTriangles();
    LocalFlipTriangles[3] = Reverse(LocalFlipTriangles[3]);
    const FDatasmithMeshAuditResult LocalFlip = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(Tetra, LocalFlipTriangles), FDatasmithMeshAuditOptions());
    TestTrue(
        TEXT("A local closed-shell flip reports LOCAL_WINDING_CONFLICT"),
        HasIssue(LocalFlip, EDatasmithMeshAuditIssueType::LocalWindingConflict));
    TestFalse(
        TEXT("A locally inconsistent shell is not mislabeled globally inward"),
        HasIssue(LocalFlip, EDatasmithMeshAuditIssueType::ClosedShellInward));
    TestTrue(
        TEXT("Localized winding region contains the deliberately flipped face"),
        GatherErrorTriangleIds(LocalFlip, EDatasmithMeshAuditIssueType::LocalWindingConflict).Contains(3));

    const TArray<FVector> NonManifoldPositions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0),
        FVector(0.0, -100.0, 0.0),
        FVector(0.0, 0.0, 100.0)};
    const TArray<FTriangleSpec> ThreeFacesOnEdge = {
        {0, 1, 2}, {1, 0, 3}, {0, 1, 4}};
    const FDatasmithMeshAuditResult NonManifold = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(NonManifoldPositions, ThreeFacesOnEdge),
        FDatasmithMeshAuditOptions());
    const FDatasmithMeshAuditIssue* NonManifoldIssue =
        FindIssue(NonManifold, EDatasmithMeshAuditIssueType::NonManifoldTopology);
    TestNotNull(TEXT("Three faces on one edge report NON_MANIFOLD_TOPOLOGY"), NonManifoldIssue);
    if (NonManifoldIssue)
    {
        TArray<int32> CompleteIds = NonManifoldIssue->ErrorTriangleIds;
        CompleteIds.Sort();
        TestTrue(
            TEXT("Non-manifold region locates all incident faces"),
            CompleteIds == TArray<int32>({0, 1, 2}));
    }

    const TArray<FVector> DegeneratePositions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(200.0, 0.0, 0.0)};
    const FDatasmithMeshAuditResult Degenerate = UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
        CreateTransientMesh(DegeneratePositions, {{0, 1, 2}}),
        FDatasmithMeshAuditOptions());
    TestTrue(
        TEXT("Collinear face reports canonical DEGENERATE_TRIANGLE"),
        HasIssue(Degenerate, EDatasmithMeshAuditIssueType::DegenerateTriangle));

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditRegionIsolationStabilityAndFiniteBoundsTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.RegionIsolationStabilityAndFiniteBounds",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditRegionIsolationStabilityAndFiniteBoundsTest::RunTest(
    const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0),
        FVector(0.0, -100.0, 0.0),
        FVector(0.0, 0.0, 100.0),
        FVector(1000.0, 0.0, 0.0),
        FVector(1100.0, 0.0, 0.0),
        FVector(1000.0, 100.0, 0.0),
        FVector(1000.0, -100.0, 0.0),
        FVector(1000.0, 0.0, 100.0)};
    const TArray<FTriangleSpec> TwoNonManifoldRegions = {
        {0, 1, 2}, {1, 0, 3}, {0, 1, 4},
        {5, 6, 7}, {6, 5, 8}, {5, 6, 9}};
    UStaticMesh* Mesh = CreateTransientMesh(Positions, TwoNonManifoldRegions);
    const FDatasmithMeshAuditResult First =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, FDatasmithMeshAuditOptions());
    const FDatasmithMeshAuditResult Second =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, FDatasmithMeshAuditOptions());

    TMap<FString, FString> FirstRegionIds;
    TMap<FString, FString> SecondRegionIds;
    for (const FDatasmithMeshAuditIssue& Issue : First.Issues)
    {
        if (Issue.IssueType != EDatasmithMeshAuditIssueType::NonManifoldTopology) continue;
        TestEqual(
            TEXT("Each disconnected non-manifold result reports its local offending-edge count"),
            Issue.Count,
            1);
        TestEqual(
            TEXT("Each disconnected non-manifold region locates its three incident faces"),
            Issue.ErrorTriangleIds.Num(),
            3);
        TestFalse(TEXT("Every non-manifold region has a stable ID"), Issue.RegionId.IsEmpty());
        FirstRegionIds.Add(TriangleIdKey(Issue), Issue.RegionId);
    }
    for (const FDatasmithMeshAuditIssue& Issue : Second.Issues)
    {
        if (Issue.IssueType == EDatasmithMeshAuditIssueType::NonManifoldTopology)
        {
            SecondRegionIds.Add(TriangleIdKey(Issue), Issue.RegionId);
        }
    }
    TestEqual(TEXT("Two disconnected non-manifold regions remain separate"), FirstRegionIds.Num(), 2);
    TestTrue(
        TEXT("RegionId is stable across repeated scans of identical Source triangle IDs"),
        FirstRegionIds.OrderIndependentCompareEqual(SecondRegionIds));

    const double NaN = std::numeric_limits<double>::quiet_NaN();
    const TArray<FVector> NonFinitePositions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(NaN, 100.0, 0.0)};
    const FDatasmithMeshAuditResult NonFinite =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
            CreateTransientMesh(NonFinitePositions, {{0, 1, 2}}),
            FDatasmithMeshAuditOptions());
    const FDatasmithMeshAuditIssue* Degenerate =
        FindIssue(NonFinite, EDatasmithMeshAuditIssueType::DegenerateTriangle);
    TestNotNull(TEXT("NaN geometry is reported as DEGENERATE_TRIANGLE"), Degenerate);
    if (Degenerate)
    {
        TestTrue(
            TEXT("NaN geometry never escapes into LocalBoundsMin"),
            IsFiniteVector(Degenerate->LocalBoundsMin));
        TestTrue(
            TEXT("NaN geometry never escapes into LocalBoundsMax"),
            IsFiniteVector(Degenerate->LocalBoundsMax));
        TestTrue(
            TEXT("Sanitized bounds remain ordered"),
            Degenerate->LocalBoundsMin.X <= Degenerate->LocalBoundsMax.X &&
                Degenerate->LocalBoundsMin.Y <= Degenerate->LocalBoundsMax.Y &&
                Degenerate->LocalBoundsMin.Z <= Degenerate->LocalBoundsMax.Z);
    }

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditCompleteRegionHighlightAndSafetyLimitTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.CompleteRegionHighlightAndSafetyLimit",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditCompleteRegionHighlightAndSafetyLimitTest::RunTest(
    const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    constexpr int32 CellsPerAxis = 10;
    TArray<FVector> Positions;
    Positions.Reserve((CellsPerAxis + 1) * (CellsPerAxis + 1));
    for (int32 Y = 0; Y <= CellsPerAxis; ++Y)
    {
        for (int32 X = 0; X <= CellsPerAxis; ++X)
        {
            Positions.Add(FVector(X * 10.0, Y * 10.0, 0.0));
        }
    }
    TArray<FTriangleSpec> Triangles;
    for (int32 Y = 0; Y < CellsPerAxis; ++Y)
    {
        for (int32 X = 0; X < CellsPerAxis; ++X)
        {
            const int32 A = Y * (CellsPerAxis + 1) + X;
            const int32 B = A + 1;
            const int32 D = (Y + 1) * (CellsPerAxis + 1) + X;
            const int32 C = D + 1;
            AddQuad(Triangles, A, B, C, D);
        }
    }
    UStaticMesh* Mesh = CreateTransientMesh(Positions, Triangles, true, true);
    FDatasmithMeshAuditOptions Options;
    Options.bReportNormalDirectionMismatch = true;
    const FDatasmithMeshAuditResult Result =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, Options);
    const FDatasmithMeshAuditIssue* Region =
        FindIssue(Result, EDatasmithMeshAuditIssueType::VertexNormalReversed);
    TestNotNull(TEXT("Connected reversed grid produces one face region"), Region);
    if (!Region) return false;

    TestEqual(
        TEXT("Complete IDs are independent of the default 64-face preview sample"),
        Region->ErrorTriangleIds.Num(),
        CellsPerAxis * CellsPerAxis * 2);
    TestEqual(TEXT("Preview sample obeys the default limit"), Region->TriangleIds.Num(), 64);
    TestTrue(TEXT("Large region reports sample truncation"), Region->bSamplesTruncated);

    UWorld* World = GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    TestNotNull(TEXT("Editor automation world is available for native highlight"), World);
    if (!World) return false;
    AStaticMeshActor* Actor = World->SpawnActor<AStaticMeshActor>();
    TestNotNull(TEXT("Transient highlight actor was created"), Actor);
    if (!Actor) return false;
    UStaticMeshComponent* Component = Actor->GetStaticMeshComponent();
    TestNotNull(TEXT("Transient highlight actor has a StaticMeshComponent"), Component);
    if (!Component)
    {
        World->DestroyActor(Actor);
        return false;
    }
    Component->SetStaticMesh(Mesh);

    const int32 FullDrawCount = UDatasmithMeshAuditLibrary::DrawStaticMeshTriangleHighlights(
        Component,
        Region->ErrorTriangleIds,
        FLinearColor::Red,
        -1.0f,
        true,
        true,
        100000);
    TestEqual(
        TEXT("Native Source-ID highlight reconstructs every face beyond the preview sample"),
        FullDrawCount,
        Region->ErrorTriangleIds.Num());

    const int32 CappedDrawCount = UDatasmithMeshAuditLibrary::DrawStaticMeshTriangleHighlights(
        Component,
        Region->ErrorTriangleIds,
        FLinearColor::Red,
        -1.0f,
        true,
        true,
        32);
    TestEqual(TEXT("Caller safety limit is enforced"), CappedDrawCount, 32);

    TArray<int32> RepeatedIds;
    RepeatedIds.Init(0, 100001);
    const int32 HardLimitedDrawCount =
        UDatasmithMeshAuditLibrary::DrawStaticMeshTriangleHighlights(
            Component,
            RepeatedIds,
            FLinearColor::Red,
            -1.0f,
            true,
            true,
            MAX_int32);
    TestTrue(TEXT("Hard highlight limit still accepts valid input"), HardLimitedDrawCount > 0);
    TestTrue(
        TEXT("Native highlight can never exceed the 100000-face hard limit"),
        HardLimitedDrawCount <= 100000);

    UDatasmithMeshAuditLibrary::ClearTriangleFaceHighlights(Component);
    World->DestroyActor(Actor);
    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditActorScaleIsolationAndUvCompatibilityTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.ActorScaleIsolationAndLegacyUv",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditActorScaleIsolationAndUvCompatibilityTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditRegressionTests;

    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0)};
    UStaticMesh* Mesh = CreateTransientMesh(Positions, {{0, 1, 2}});
    FDatasmithMeshAuditOptions NegativeScaleOptions;
    NegativeScaleOptions.WorldScale = FVector(-1.0, 1.0, 1.0);
    const FDatasmithMeshAuditResult NativeNegativeScale =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, NegativeScaleOptions);
    TestFalse(
        TEXT("Native source-mesh audit never emits actor-instance scale findings"),
        HasIssue(NativeNegativeScale, EDatasmithMeshAuditIssueType::ActorNegativeScale));
    TestFalse(
        TEXT("Actor negative scale does not contaminate source normal results"),
        HasIssue(NativeNegativeScale, EDatasmithMeshAuditIssueType::VertexNormalReversed));

    USceneComponent* MirroredComponent =
        NewObject<USceneComponent>(GetTransientPackage(), NAME_None, RF_Transient);
    MirroredComponent->SetRelativeScale3D(FVector(-1.0, 2.0, 3.0));
    MirroredComponent->UpdateComponentToWorld();
    const FDatasmithComponentTransformAuditResult MirroredTransform =
        UDatasmithMeshAuditLibrary::AnalyzeSceneComponentTransform(MirroredComponent);
    TestTrue(TEXT("Native component transform audit succeeds"), MirroredTransform.Success);
    TestTrue(
        TEXT("Complete component matrix determinant remains negative"),
        MirroredTransform.Determinant < 0.0);
    TestTrue(
        TEXT("Negative component matrix determinant is reported separately"),
        MirroredTransform.bOrientationReversing);

    USceneComponent* PositiveDeterminantComponent =
        NewObject<USceneComponent>(GetTransientPackage(), NAME_None, RF_Transient);
    PositiveDeterminantComponent->SetRelativeScale3D(FVector(-1.0, -2.0, 3.0));
    PositiveDeterminantComponent->UpdateComponentToWorld();
    const FDatasmithComponentTransformAuditResult PositiveTransform =
        UDatasmithMeshAuditLibrary::AnalyzeSceneComponentTransform(
            PositiveDeterminantComponent);
    TestTrue(TEXT("Positive determinant component audit succeeds"), PositiveTransform.Success);
    TestTrue(TEXT("Two reflected axes have positive determinant"), PositiveTransform.Determinant > 0.0);
    TestFalse(
        TEXT("A positive determinant is not orientation reversing"),
        PositiveTransform.bOrientationReversing);

    FDatasmithMeshAuditOptions MissingUVOptions;
    MissingUVOptions.UVChannel = 1;
    const FDatasmithMeshAuditResult MissingUV =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, MissingUVOptions);
    TestTrue(
        TEXT("Legacy UV issue enums remain available in 0.9.5"),
        HasIssue(MissingUV, EDatasmithMeshAuditIssueType::MissingUvChannel));

    return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
