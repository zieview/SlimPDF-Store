# Store 동봉 엔진 후보

이 파일은 배포 고지 준비 기록이며 최종 라이선스 검토 완료를 의미하지 않는다.

- Ghostscript 10.05.0: GNU AGPL v3 경로. 실행 파일과 DLL, Resource, lib,
  iccprofiles, doc를 준비했다. 설치 프로그램·제거 프로그램은 동봉하지 않는다.
- Tesseract: 로컬 설치본의 실행 파일·DLL 및 eng/kor/osd 학습 데이터를 준비했다.
  엔진의 Apache 2.0 라이선스 외에도 각 DLL의 라이선스를 별도로 확인해야 한다.
- 파일별 SHA-256: vendor/store-engines/inventory.json.

동봉 후보 실행 확인: Ghostscript --version = 10.05.0, Tesseract --list-langs =
eng, kor, osd. 사용자 PC의 설치 경로 대신 후보 폴더에서 직접 실행했다.
깨끗한 Windows VM/MSIX 설치 테스트는 아직 수행하지 않았다.

최종 제출 전:
1. Tesseract 배포 빌드의 정확한 버전·출처 및 DLL별 고지 전문 확보.
2. Ghostscript와 PyMuPDF/MuPDF의 배포 버전에 대응하는 소스 제공 자료 확보.
3. SlimPDF 대응 소스와 빌드 방법을 검증하고 공개 다운로드 위치 제공.
4. 최종 MSIX에 필요한 라이선스 전문·고지·소스 안내를 포함.
5. 시스템 설치 없이 동봉 엔진으로 실제 압축/OCR을 검증.

기존 사내 Inno Setup 배포판은 이 후보 폴더를 사용하지 않는다.
