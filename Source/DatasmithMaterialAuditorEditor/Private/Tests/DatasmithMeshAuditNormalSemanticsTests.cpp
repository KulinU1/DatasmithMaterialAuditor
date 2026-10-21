#if WITH_DEV_AUTOMATION_TESTS

#include "DatasmithMeshAuditLibrary.h"

#include "Engine/StaticMesh.h"
#include "MeshDescription.h"
#include "Misc/AutomationTest.h"
#include "StaticMeshAttributes.h"

#include <limits>

namespace DatasmithMeshAuditNormalSemanticsTests
{
struct FTriangleSpec
{
    int32 VertexIndices[3] = {0, 0, 0};
    FVector Normals[3] = {FVector::ZeroVector, FVector::ZeroVector, FVector::ZeroVector};
};

static FTriangleSpec MakeTriangle(
    const int32 A,
    const int32 B,
    const int32 C,
    const FVector& NormalA,
    const FVector& NormalB,
    const FVector& NormalC)
{
    FTriangleSpec Triangle;
    Triangle.VertexIndices[0] = A;
    Triangle.VertexIndices[1] = B;
    Triangle.VertexIndices[2] = C;
    Triangle.Normals[0] = NormalA;
    Triangle.Normals[1] = NormalB;
    Triangle.Normals[2] = NormalC;
    return Triangle;
}

static FTriangleSpec MakeFlatTriangle(
    const int32 A,
    const int32 B,
    const int32 C,
    const FVector& Normal)
{
    return MakeTriangle(A, B, C, Normal, Normal, Normal);
}

static UStaticMesh* CreateTransientMesh(
    const TArray<FVector>& Positions,
    const TArray<FTriangleSpec>& Triangles,
    const bool bRecomputeNormals = false)
{
    UStaticMesh* StaticMesh = NewObject<UStaticMesh>(GetTransientPackage(), NAME_None, RF_Transient);
    StaticMesh->AddSourceModel();
    StaticMesh->GetSourceModel(0).BuildSettings.bRecomputeNormals = bRecomputeNormals;
    FMeshDescription* MeshDescription = StaticMesh->CreateMeshDescription(0);
    check(MeshDescription != nullptr);

    FStaticMeshAttributes Attributes(*MeshDescription);
    Attributes.Register();
    TVertexAttributesRef<FVector3f> VertexPositions = Attributes.GetVertexPositions();
    TVertexInstanceAttributesRef<FVector3f> VertexNormals = Attributes.GetVertexInstanceNormals();
    TVertexInstanceAttributesRef<FVector2f> VertexUVs = Attributes.GetVertexInstanceUVs();
    VertexUVs.SetNumChannels(1);

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
        TArray<FVertexInstanceID, TInlineAllocator<3>> InstanceIds;
        for (int32 CornerIndex = 0; CornerIndex < 3; ++CornerIndex)
        {
            const FVertexInstanceID InstanceId = MeshDescription->CreateVertexInstance(
                VertexIds[Triangle.VertexIndices[CornerIndex]]);
            VertexNormals[InstanceId] = FVector3f(Triangle.Normals[CornerIndex]);
            VertexUVs.Set(
                InstanceId,
                0,
                CornerIndex == 0
                    ? FVector2f(0.0f, 0.0f)
                    : (CornerIndex == 1 ? FVector2f(1.0f, 0.0f) : FVector2f(0.0f, 1.0f)));
            InstanceIds.Add(InstanceId);
        }
        MeshDescription->CreateTriangle(PolygonGroupId, InstanceIds);
    }

    StaticMesh->CommitMeshDescription(0);
    return StaticMesh;
}

static int32 SumCount(
    const FDatasmithMeshAuditResult& Result,
    const EDatasmithMeshAuditIssueType Type,
    const TOptional<EDatasmithMeshAuditSeverity> Severity = {})
{
    int32 Total = 0;
    for (const FDatasmithMeshAuditIssue& Issue : Result.Issues)
    {
        if (Issue.IssueType == Type && (!Severity.IsSet() || Issue.Severity == Severity.GetValue()))
        {
            Total += Issue.Count;
        }
    }
    return Total;
}

static FDatasmithMeshAuditResult AnalyzeTriangleNormals(
    const FVector& NormalA,
    const FVector& NormalB,
    const FVector& NormalC)
{
    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0)};
    UStaticMesh* Mesh = CreateTransientMesh(
        Positions,
        {MakeTriangle(0, 1, 2, NormalA, NormalB, NormalC)});
    FDatasmithMeshAuditOptions Options;
    Options.bReportNormalDirectionMismatch = true;
    return UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(Mesh, Options);
}

static FDatasmithMeshAuditResult AnalyzeSingleTriangle(const FVector& Normal)
{
    return AnalyzeTriangleNormals(Normal, Normal, Normal);
}
} // namespace DatasmithMeshAuditNormalSemanticsTests

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditVertexNormalClassificationTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.VertexNormalClassification",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditVertexNormalClassificationTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditNormalSemanticsTests;

    const FVector HealthyNormal(0.0, 0.0, -1.0);
    const FDatasmithMeshAuditResult Healthy = AnalyzeSingleTriangle(HealthyNormal);
    TestTrue(TEXT("Healthy open triangle is analyzable"), Healthy.Success);
    TestEqual(
        TEXT("Healthy vertex normals are not invalid"),
        SumCount(Healthy, EDatasmithMeshAuditIssueType::InvalidNormal),
        0);
    TestEqual(
        TEXT("Healthy vertex normals are not reversed"),
        SumCount(Healthy, EDatasmithMeshAuditIssueType::VertexNormalReversed),
        0);

    const FDatasmithMeshAuditResult Reversed = AnalyzeTriangleNormals(
        FVector(0.0, 0.0, 1.0),
        HealthyNormal,
        HealthyNormal);
    TestEqual(
        TEXT("Vertex-instance normals with dot below -0.2 are Errors"),
        SumCount(
            Reversed,
            EDatasmithMeshAuditIssueType::VertexNormalReversed,
            EDatasmithMeshAuditSeverity::Error),
        1);

    const FVector NearPerpendicular = FVector(1.0, 0.0, 0.05).GetSafeNormal();
    const FDatasmithMeshAuditResult NearZero = AnalyzeTriangleNormals(
        NearPerpendicular,
        HealthyNormal,
        HealthyNormal);
    TestEqual(
        TEXT("Near-perpendicular vertex-instance normals are Warnings"),
        SumCount(
            NearZero,
            EDatasmithMeshAuditIssueType::VertexNormalReversed,
            EDatasmithMeshAuditSeverity::Warning),
        1);
    TestEqual(
        TEXT("Near-perpendicular normals are not promoted to Error"),
        SumCount(
            NearZero,
            EDatasmithMeshAuditIssueType::VertexNormalReversed,
            EDatasmithMeshAuditSeverity::Error),
        0);

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditInvalidNormalClassificationTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.InvalidNormalClassification",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditInvalidNormalClassificationTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditNormalSemanticsTests;

    const FDatasmithMeshAuditResult Zero = AnalyzeSingleTriangle(FVector::ZeroVector);
    TestTrue(
        TEXT("Zero vertex-instance normals are INVALID_NORMAL"),
        SumCount(Zero, EDatasmithMeshAuditIssueType::InvalidNormal) > 0);

    const double NaN = std::numeric_limits<double>::quiet_NaN();
    const FDatasmithMeshAuditResult NotFinite = AnalyzeSingleTriangle(FVector(NaN, 0.0, 0.0));
    TestTrue(
        TEXT("NaN vertex-instance normals are INVALID_NORMAL"),
        SumCount(NotFinite, EDatasmithMeshAuditIssueType::InvalidNormal) > 0);

    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0)};
    UStaticMesh* NonUnitMesh = CreateTransientMesh(
        Positions,
        {MakeFlatTriangle(0, 1, 2, FVector(0.0, 0.0, -5.0))});
    const FDatasmithMeshAuditResult NonUnit =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(
            NonUnitMesh, FDatasmithMeshAuditOptions());
    TestTrue(
        TEXT("Non-unit vertex-instance normals are canonical INVALID_NORMAL findings"),
        SumCount(NonUnit, EDatasmithMeshAuditIssueType::InvalidNormal) > 0);
    TestEqual(
        TEXT("Invalid normals are not mislabeled as the directional issue"),
        SumCount(NonUnit, EDatasmithMeshAuditIssueType::VertexNormalReversed),
        0);

    return true;
}

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
    FDatasmithMeshAuditRecomputedNormalViewportSemanticsTest,
    "DatasmithMaterialAuditor.MeshAudit.V095.RecomputedNormalsIgnoreStaleSourceVI",
    EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FDatasmithMeshAuditRecomputedNormalViewportSemanticsTest::RunTest(const FString& Parameters)
{
    (void)Parameters;
    using namespace DatasmithMeshAuditNormalSemanticsTests;

    const TArray<FVector> Positions = {
        FVector(0.0, 0.0, 0.0),
        FVector(100.0, 0.0, 0.0),
        FVector(0.0, 100.0, 0.0)};
    FDatasmithMeshAuditOptions Options;
    Options.bReportNormalDirectionMismatch = true;

    UStaticMesh* ReversedSourceMesh = CreateTransientMesh(
        Positions,
        {MakeFlatTriangle(0, 1, 2, FVector(0.0, 0.0, 1.0))},
        true);
    const FDatasmithMeshAuditResult ReversedSource =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(ReversedSourceMesh, Options);
    TestEqual(
        TEXT("Recompute Normals prevents stale reversed Source VI normals from becoming a viewport error"),
        SumCount(ReversedSource, EDatasmithMeshAuditIssueType::VertexNormalReversed),
        0);

    UStaticMesh* ZeroSourceMesh = CreateTransientMesh(
        Positions,
        {MakeFlatTriangle(0, 1, 2, FVector::ZeroVector)},
        true);
    const FDatasmithMeshAuditResult ZeroSource =
        UDatasmithMeshAuditLibrary::AnalyzeStaticMesh(ZeroSourceMesh, Options);
    TestEqual(
        TEXT("Unused zero Source VI normals are not viewport Error findings when normals are recomputed"),
        SumCount(
            ZeroSource,
            EDatasmithMeshAuditIssueType::InvalidNormal,
            EDatasmithMeshAuditSeverity::Error),
        0);
    TestTrue(
        TEXT("Recomputed meshes may retain a non-blocking source-data Warning for zero VI normals"),
        SumCount(
            ZeroSource,
            EDatasmithMeshAuditIssueType::InvalidNormal,
            EDatasmithMeshAuditSeverity::Warning) > 0);
    TestEqual(
        TEXT("Recompute Normals still leaves the healthy open-surface topology state"),
        SumCount(ZeroSource, EDatasmithMeshAuditIssueType::OpenMeshDirectionUnknown),
        1);

    return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
